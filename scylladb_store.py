"""
ScyllaDB implementation of LangGraph's BaseStore interface.

This module provides AsyncScyllaDBStore, which implements the same interface
as LangGraph's AsyncPostgresStore but uses ScyllaDB as the backend.
"""

import asyncio
import json
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Iterable,
    Literal,
    Sequence,
    TypedDict,
)

from cassandra.cluster import Cluster, Session, ExecutionProfile, EXEC_PROFILE_DEFAULT
from cassandra.policies import WhiteListRoundRobinPolicy, DowngradingConsistencyRetryPolicy
from cassandra.query import SimpleStatement, BatchStatement, BatchType, ConsistencyLevel
from cassandra.concurrent import execute_concurrent_with_args

try:
    from langgraph.store.base import (
        BaseStore,
        Item,
        SearchItem,
        Op,
        GetOp,
        PutOp,
        SearchOp,
        ListNamespacesOp,
        Result,
    )
except ImportError:
    # Fallback definitions if langgraph not installed
    from dataclasses import dataclass
    from typing import Union

    @dataclass
    class Item:
        """Stored item with metadata."""
        value: dict[str, Any]
        key: str
        namespace: tuple[str, ...]
        created_at: datetime
        updated_at: datetime

    @dataclass
    class SearchItem(Item):
        """Search result with similarity score."""
        score: float = 1.0

    @dataclass
    class GetOp:
        namespace: tuple[str, ...]
        key: str
        refresh_ttl: bool = True

    @dataclass
    class PutOp:
        namespace: tuple[str, ...]
        key: str
        value: dict[str, Any]
        index: Literal[False] | list[str] | None = None
        ttl: float | None = None

    @dataclass
    class SearchOp:
        namespace_prefix: tuple[str, ...]
        filter: dict[str, Any] | None = None
        limit: int = 10
        offset: int = 0
        query: str | None = None
        refresh_ttl: bool = True

    @dataclass
    class ListNamespacesOp:
        match_conditions: tuple[tuple[str, ...] | None, tuple[str, ...] | None, int | None]
        max_depth: int | None = None
        limit: int = 100
        offset: int = 0

    Op = Union[GetOp, PutOp, SearchOp, ListNamespacesOp]
    Result = Item | list[Item] | list[tuple[str, ...]] | None

    class BaseStore:
        """Base class for stores."""
        supports_ttl: bool = False


logger = logging.getLogger(__name__)


class PoolConfig(TypedDict, total=False):
    """Configuration for connection pool."""
    core_connections_per_host: int
    max_connections_per_host: int
    protocol_version: int
    port: int


class TTLConfig(TypedDict, total=False):
    """Configuration for TTL behavior."""
    refresh_on_read: bool  # Refresh TTL on GET/SEARCH (default: True)
    default_ttl: float | None  # Default TTL in minutes
    sweep_interval_minutes: int | None  # Interval between sweeps


class ScyllaIndexConfig(TypedDict, total=False):
    """Configuration for secondary indexes and vector search."""
    enable_sai: bool  # Enable Storage-Attached Indexes
    indexed_fields: list[str]  # Fields to create secondary indexes on


class NOT_PROVIDED:
    """Sentinel value for optional parameters."""
    pass


class AsyncScyllaDBStore(BaseStore):
    """
    Async ScyllaDB implementation of LangGraph's BaseStore interface.

    Provides the same API as AsyncPostgresStore but uses ScyllaDB/Cassandra
    as the backend database.

    Schema:
        - store table: Main key-value storage with TTL support
        - Secondary indexes for efficient queries (if configured)

    Features:
        - Hierarchical namespaces
        - TTL support with automatic expiration
        - Batch operations
        - Filter-based search
        - Namespace listing and filtering

    Example:
        async with AsyncScyllaDBStore.from_contact_points(
            contact_points=["127.0.0.1"],
            keyspace="langgraph_store"
        ) as store:
            await store.setup()
            await store.aput(("users", "123"), "profile", {"name": "Alice"})
            item = await store.aget(("users", "123"), "profile")
    """

    supports_ttl: bool = True

    def __init__(
        self,
        session: Session,
        keyspace: str,
        *,
        deserializer: Callable[[str], dict[str, Any]] | None = None,
        index: ScyllaIndexConfig | None = None,
        ttl: TTLConfig | None = None,
    ) -> None:
        """
        Initialize ScyllaDB store.

        Args:
            session: ScyllaDB/Cassandra session
            keyspace: Keyspace name
            deserializer: Optional JSON deserializer
            index: Optional index configuration
            ttl: Optional TTL configuration
        """
        self.session = session
        self.keyspace = keyspace
        self.deserializer = deserializer or json.loads
        self.index_config = index or {}
        self.ttl_config = ttl or {}
        self.lock = asyncio.Lock()
        self.loop = asyncio.get_event_loop()
        self._ttl_sweeper_task: asyncio.Task | None = None
        self._ttl_stop_event = asyncio.Event()

        # Prepared statements cache
        self._prepared_statements: dict[str, Any] = {}

    @classmethod
    @asynccontextmanager
    async def from_contact_points(
        cls,
        contact_points: list[str],
        keyspace: str,
        *,
        pool_config: PoolConfig | None = None,
        index: ScyllaIndexConfig | None = None,
        ttl: TTLConfig | None = None,
    ) -> AsyncIterator["AsyncScyllaDBStore"]:
        """
        Create AsyncScyllaDBStore from contact points.

        Args:
            contact_points: List of ScyllaDB node addresses
            keyspace: Keyspace name
            pool_config: Optional connection pool configuration
            index: Optional index configuration
            ttl: Optional TTL configuration

        Yields:
            AsyncScyllaDBStore instance

        Example:
            async with AsyncScyllaDBStore.from_contact_points(
                contact_points=["127.0.0.1", "127.0.0.2"],
                keyspace="my_store"
            ) as store:
                await store.setup()
                # Use store...
        """
        pool_config = pool_config or {}

        # Create cluster - use simplest configuration
        cluster = Cluster(
            contact_points=contact_points,
            **pool_config
        )

        store = None
        try:
            # Connect to cluster
            session = await asyncio.get_event_loop().run_in_executor(
                None, cluster.connect
            )

            store = cls(
                session=session,
                keyspace=keyspace,
                index=index,
                ttl=ttl,
            )

            yield store

        finally:
            # Cleanup
            if store is not None and hasattr(store, '_ttl_sweeper_task') and store._ttl_sweeper_task:
                await store.stop_ttl_sweeper()

            await asyncio.get_event_loop().run_in_executor(
                None, cluster.shutdown
            )

    async def setup(self) -> None:
        """
        Set up keyspace and tables. Must be called before first use.

        Creates:
            - Keyspace (if not exists)
            - store table with primary key (prefix, key)
            - Secondary indexes (if configured)

        Idempotent - safe to call multiple times.
        """
        # Create keyspace if not exists
        await self._execute_async(f"""
            CREATE KEYSPACE IF NOT EXISTS {self.keyspace}
            WITH replication = {{
                'class': 'SimpleStrategy',
                'replication_factor': 3
            }}
        """)

        # Set keyspace
        self.session.set_keyspace(self.keyspace)

        # Create main store table
        await self._execute_async("""
            CREATE TABLE IF NOT EXISTS store (
                prefix text,
                key text,
                value text,
                created_at timestamp,
                updated_at timestamp,
                ttl_minutes int,
                PRIMARY KEY (prefix, key)
            )
        """)

        # Create secondary indexes if configured
        if self.index_config.get("enable_sai"):
            indexed_fields = self.index_config.get("indexed_fields", [])
            for field in indexed_fields:
                safe_field = field.replace(".", "_")
                try:
                    await self._execute_async(f"""
                        CREATE INDEX IF NOT EXISTS store_{safe_field}_idx
                        ON store (value)
                        USING 'sai'
                    """)
                except Exception as e:
                    logger.warning(f"Failed to create SAI index for {field}: {e}")

        logger.info(f"ScyllaDB store setup complete in keyspace '{self.keyspace}'")

    async def aget(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        refresh_ttl: bool | None = None,
    ) -> Item | None:
        """
        Retrieve a single item asynchronously.

        Args:
            namespace: Hierarchical path (e.g., ("users", "123"))
            key: Unique identifier within namespace
            refresh_ttl: Whether to refresh TTL on read (uses config default if None)

        Returns:
            Item object or None if not found
        """
        self._validate_namespace(namespace)

        prefix = self._namespace_to_prefix(namespace)
        should_refresh = refresh_ttl if refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

        # Build query
        query = "SELECT * FROM store WHERE prefix = %s AND key = %s"

        # Execute query
        result = await self._execute_async(query, (prefix, key))

        if not result:
            return None

        row = result[0]

        # Refresh TTL if requested and TTL is set
        if should_refresh and row.ttl_minutes:
            await self._refresh_ttl(prefix, key, row.ttl_minutes)

        return self._row_to_item(row, namespace, key)

    async def aput(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        index: Literal[False] | list[str] | None = None,
        *,
        ttl: float | None | type[NOT_PROVIDED] = NOT_PROVIDED,
    ) -> None:
        """
        Store or update an item asynchronously.

        Args:
            namespace: Hierarchical path
            key: Unique identifier
            value: Dictionary to store (must be JSON-serializable)
            index: Controls field indexing (not fully implemented for ScyllaDB)
            ttl: Time-to-live in minutes (None = no expiration)
        """
        self._validate_namespace(namespace)

        prefix = self._namespace_to_prefix(namespace)
        value_json = json.dumps(value)
        now = datetime.now(timezone.utc)

        # Determine TTL
        ttl_minutes = None
        ttl_seconds = None

        if ttl is not NOT_PROVIDED:
            if ttl is not None:
                ttl_minutes = int(ttl)
                ttl_seconds = int(ttl * 60)
        elif self.ttl_config.get("default_ttl"):
            ttl_minutes = int(self.ttl_config["default_ttl"])
            ttl_seconds = int(ttl_minutes * 60)

        # Build INSERT query with TTL
        if ttl_seconds:
            query = """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes)
                VALUES (%s, %s, %s, %s, %s, %s)
                USING TTL %s
            """
            params = (prefix, key, value_json, now, now, ttl_minutes, ttl_seconds)
        else:
            query = """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            params = (prefix, key, value_json, now, now, ttl_minutes)

        await self._execute_async(query, params)

    async def adelete(
        self,
        namespace: tuple[str, ...],
        key: str,
    ) -> None:
        """
        Delete an item asynchronously.

        Args:
            namespace: Hierarchical path
            key: Item identifier
        """
        self._validate_namespace(namespace)

        prefix = self._namespace_to_prefix(namespace)
        query = "DELETE FROM store WHERE prefix = %s AND key = %s"

        await self._execute_async(query, (prefix, key))

    async def asearch(
        self,
        namespace_prefix: tuple[str, ...],
        /,
        *,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
        refresh_ttl: bool | None = None,
    ) -> list[SearchItem]:
        """
        Search for items within a namespace prefix.

        Args:
            namespace_prefix: Path prefix to search within
            query: Natural language query (not fully supported in ScyllaDB)
            filter: Key-value pairs for filtering results
            limit: Maximum items to return
            offset: Number of items to skip (pagination)
            refresh_ttl: Whether to refresh TTL on read

        Returns:
            List of SearchItem objects

        Note:
            Vector/semantic search is not supported. Filters are applied
            in Python after fetching results. For better performance,
            consider using ScyllaDB's SAI indexes.
        """
        self._validate_namespace(namespace_prefix)

        prefix = self._namespace_to_prefix(namespace_prefix)
        should_refresh = refresh_ttl if refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

        # Query all items with matching prefix
        # Note: Using >= and < for prefix matching
        prefix_end = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else "\uffff"
        query_str = "SELECT * FROM store WHERE prefix >= %s AND prefix < %s ALLOW FILTERING"

        results = await self._execute_async(query_str, (prefix, prefix_end))

        if not results:
            return []

        # Apply filters in Python (ScyllaDB doesn't support complex JSON filtering easily)
        items = []
        for row in results:
            value = json.loads(row.value)

            # Apply filters
            if filter and not self._matches_filter(value, filter):
                continue

            # Convert namespace
            ns = self._prefix_to_namespace(row.prefix)

            # Refresh TTL if needed
            if should_refresh and row.ttl_minutes:
                await self._refresh_ttl(row.prefix, row.key, row.ttl_minutes)

            items.append(SearchItem(
                value=value,
                key=row.key,
                namespace=ns,
                created_at=row.created_at,
                updated_at=row.updated_at,
                score=1.0  # No vector search, all scores are 1.0
            ))

        # Apply offset and limit
        return items[offset:offset + limit]

    async def alist_namespaces(
        self,
        *,
        prefix: tuple[str, ...] | None = None,
        suffix: tuple[str, ...] | None = None,
        max_depth: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[str, ...]]:
        """
        List and filter namespaces in the store.

        Args:
            prefix: Filter namespaces starting with this path
            suffix: Filter namespaces ending with this path
            max_depth: Return namespaces up to this depth (truncates deeper ones)
            limit: Maximum namespaces to return
            offset: Pagination offset

        Returns:
            List of namespace tuples
        """
        # Get all unique prefixes
        query = "SELECT DISTINCT prefix FROM store"
        results = await self._execute_async(query)

        if not results:
            return []

        # Convert to namespaces
        namespaces = [self._prefix_to_namespace(row.prefix) for row in results]

        # Apply filters
        filtered = []
        for ns in namespaces:
            # Check prefix
            if prefix and not ns[:len(prefix)] == prefix:
                continue

            # Check suffix
            if suffix and not ns[-len(suffix):] == suffix:
                continue

            # Apply max_depth
            if max_depth is not None and len(ns) > max_depth:
                ns = ns[:max_depth]

            if ns not in filtered:
                filtered.append(ns)

        # Sort and apply pagination
        filtered.sort()
        return filtered[offset:offset + limit]

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """
        Execute multiple operations in a single batch.

        Args:
            ops: Iterable of operations (GetOp, PutOp, SearchOp, ListNamespacesOp)

        Returns:
            List of results corresponding to each operation
        """
        ops_list = list(ops)
        results: list[Result] = [None] * len(ops_list)

        # Group operations by type
        grouped: dict[type, list[tuple[int, Op]]] = defaultdict(list)
        for idx, op in enumerate(ops_list):
            grouped[type(op)].append((idx, op))

        # Execute each group
        tasks = []

        if GetOp in grouped:
            tasks.append(self._batch_get_ops(grouped[GetOp], results))

        if PutOp in grouped:
            tasks.append(self._batch_put_ops(grouped[PutOp], results))

        if SearchOp in grouped:
            tasks.append(self._batch_search_ops(grouped[SearchOp], results))

        if ListNamespacesOp in grouped:
            tasks.append(self._batch_list_namespaces_ops(grouped[ListNamespacesOp], results))

        # Wait for all operations
        await asyncio.gather(*tasks)

        return results

    async def sweep_ttl(self) -> int:
        """
        Manually delete expired items based on TTL.

        Note: ScyllaDB automatically handles TTL expiration, so this
        method returns 0 as there's nothing to sweep manually.

        Returns:
            Number of items deleted (always 0 for ScyllaDB)
        """
        # ScyllaDB handles TTL automatically via tombstones
        # No manual sweeping needed
        logger.info("ScyllaDB handles TTL automatically - no manual sweep needed")
        return 0

    async def start_ttl_sweeper(
        self,
        sweep_interval_minutes: int | None = None
    ) -> asyncio.Task[None]:
        """
        Start a background task for TTL sweeping.

        Note: ScyllaDB handles TTL automatically, so this is a no-op
        that returns a dummy task. Included for API compatibility.

        Args:
            sweep_interval_minutes: Interval between sweeps (ignored)

        Returns:
            asyncio.Task that does nothing
        """
        async def _dummy_sweeper():
            while not self._ttl_stop_event.is_set():
                await asyncio.sleep(60)

        if self._ttl_sweeper_task is None:
            self._ttl_sweeper_task = asyncio.create_task(_dummy_sweeper())

        return self._ttl_sweeper_task

    async def stop_ttl_sweeper(
        self,
        timeout: float | None = None
    ) -> bool:
        """
        Stop the TTL sweeper task gracefully.

        Args:
            timeout: Maximum time to wait (seconds)

        Returns:
            True if stopped successfully, False if timed out
        """
        if self._ttl_sweeper_task is None:
            return True

        self._ttl_stop_event.set()

        try:
            await asyncio.wait_for(self._ttl_sweeper_task, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            self._ttl_sweeper_task.cancel()
            return False
        finally:
            self._ttl_sweeper_task = None
            self._ttl_stop_event.clear()

    async def __aenter__(self) -> "AsyncScyllaDBStore":
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context manager and stop TTL sweeper."""
        if self._ttl_sweeper_task:
            self._ttl_stop_event.set()

    # Synchronous wrappers (for BaseStore compatibility)

    def get(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        refresh_ttl: bool | None = None,
    ) -> Item | None:
        """Synchronous wrapper for aget()."""
        return asyncio.run_coroutine_threadsafe(
            self.aget(namespace, key, refresh_ttl=refresh_ttl),
            self.loop
        ).result()

    def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        index: Literal[False] | list[str] | None = None,
        *,
        ttl: float | None | type[NOT_PROVIDED] = NOT_PROVIDED,
    ) -> None:
        """Synchronous wrapper for aput()."""
        return asyncio.run_coroutine_threadsafe(
            self.aput(namespace, key, value, index, ttl=ttl),
            self.loop
        ).result()

    def delete(
        self,
        namespace: tuple[str, ...],
        key: str,
    ) -> None:
        """Synchronous wrapper for adelete()."""
        return asyncio.run_coroutine_threadsafe(
            self.adelete(namespace, key),
            self.loop
        ).result()

    def search(
        self,
        namespace_prefix: tuple[str, ...],
        /,
        *,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
        refresh_ttl: bool | None = None,
    ) -> list[SearchItem]:
        """Synchronous wrapper for asearch()."""
        return asyncio.run_coroutine_threadsafe(
            self.asearch(
                namespace_prefix,
                query=query,
                filter=filter,
                limit=limit,
                offset=offset,
                refresh_ttl=refresh_ttl,
            ),
            self.loop
        ).result()

    def list_namespaces(
        self,
        *,
        prefix: tuple[str, ...] | None = None,
        suffix: tuple[str, ...] | None = None,
        max_depth: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[str, ...]]:
        """Synchronous wrapper for alist_namespaces()."""
        return asyncio.run_coroutine_threadsafe(
            self.alist_namespaces(
                prefix=prefix,
                suffix=suffix,
                max_depth=max_depth,
                limit=limit,
                offset=offset,
            ),
            self.loop
        ).result()

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """Synchronous wrapper for abatch()."""
        return asyncio.run_coroutine_threadsafe(
            self.abatch(ops),
            self.loop
        ).result()

    # Internal helper methods

    async def _execute_async(self, query: str, parameters: tuple | None = None) -> list:
        """Execute a CQL query asynchronously."""
        def _execute():
            if parameters:
                return list(self.session.execute(query, parameters))
            return list(self.session.execute(query))

        return await self.loop.run_in_executor(None, _execute)

    def _namespace_to_prefix(self, namespace: tuple[str, ...]) -> str:
        """Convert namespace tuple to dot-separated prefix string."""
        return ".".join(namespace)

    def _prefix_to_namespace(self, prefix: str) -> tuple[str, ...]:
        """Convert dot-separated prefix string to namespace tuple."""
        return tuple(prefix.split("."))

    def _validate_namespace(self, namespace: tuple[str, ...]) -> None:
        """Validate namespace tuple."""
        if not namespace:
            raise ValueError("Namespace cannot be empty")

        if not all(isinstance(label, str) for label in namespace):
            raise ValueError("All namespace labels must be strings")

        if any("." in label for label in namespace):
            raise ValueError("Namespace labels cannot contain periods ('.')")

        if any(not label for label in namespace):
            raise ValueError("Namespace labels cannot be empty strings")

        if namespace[0] == "langgraph":
            raise ValueError("Root namespace label cannot be 'langgraph'")

    def _row_to_item(
        self,
        row,
        namespace: tuple[str, ...],
        key: str,
    ) -> Item:
        """Convert database row to Item object."""
        return Item(
            value=json.loads(row.value),
            key=key,
            namespace=namespace,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _matches_filter(self, value: dict[str, Any], filter: dict[str, Any]) -> bool:
        """Check if value matches filter criteria."""
        for key, condition in filter.items():
            if isinstance(condition, dict):
                # Operator-based filter
                field_value = self._get_nested_value(value, key)

                for op, target in condition.items():
                    if op == "$eq" and field_value != target:
                        return False
                    elif op == "$ne" and field_value == target:
                        return False
                    elif op == "$gt" and not (field_value > target):
                        return False
                    elif op == "$gte" and not (field_value >= target):
                        return False
                    elif op == "$lt" and not (field_value < target):
                        return False
                    elif op == "$lte" and not (field_value <= target):
                        return False
            else:
                # Simple equality filter
                field_value = self._get_nested_value(value, key)
                if field_value != condition:
                    return False

        return True

    def _get_nested_value(self, obj: dict[str, Any], path: str) -> Any:
        """Get nested value from object using dot notation."""
        keys = path.split(".")
        current = obj

        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                return None

        return current

    async def _refresh_ttl(self, prefix: str, key: str, ttl_minutes: int) -> None:
        """Refresh TTL for an item."""
        query = """
            UPDATE store USING TTL %s
            SET updated_at = %s
            WHERE prefix = %s AND key = %s
        """
        now = datetime.now(timezone.utc)
        ttl_seconds = ttl_minutes * 60

        await self._execute_async(query, (ttl_seconds, now, prefix, key))

    # Batch operation helpers

    async def _batch_get_ops(
        self,
        get_ops: Sequence[tuple[int, GetOp]],
        results: list[Result],
    ) -> None:
        """Execute batch GET operations."""
        tasks = []
        for idx, op in get_ops:
            tasks.append((idx, self.aget(op.namespace, op.key, refresh_ttl=op.refresh_ttl)))

        for idx, task in tasks:
            results[idx] = await task

    async def _batch_put_ops(
        self,
        put_ops: Sequence[tuple[int, PutOp]],
        results: list[Result],
    ) -> None:
        """Execute batch PUT operations."""
        for idx, op in put_ops:
            await self.aput(op.namespace, op.key, op.value, op.index, ttl=op.ttl)
            results[idx] = None

    async def _batch_search_ops(
        self,
        search_ops: Sequence[tuple[int, SearchOp]],
        results: list[Result],
    ) -> None:
        """Execute batch SEARCH operations."""
        tasks = []
        for idx, op in search_ops:
            tasks.append((
                idx,
                self.asearch(
                    op.namespace_prefix,
                    query=op.query,
                    filter=op.filter,
                    limit=op.limit,
                    offset=op.offset,
                    refresh_ttl=op.refresh_ttl,
                )
            ))

        for idx, task in tasks:
            results[idx] = await task

    async def _batch_list_namespaces_ops(
        self,
        list_ops: Sequence[tuple[int, ListNamespacesOp]],
        results: list[Result],
    ) -> None:
        """Execute batch LIST NAMESPACES operations."""
        for idx, op in list_ops:
            prefix, suffix, max_depth = op.match_conditions
            results[idx] = await self.alist_namespaces(
                prefix=prefix,
                suffix=suffix,
                max_depth=max_depth,
                limit=op.limit,
                offset=op.offset,
            )
