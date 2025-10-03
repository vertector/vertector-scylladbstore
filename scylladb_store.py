"""
ScyllaDB implementation of LangGraph's BaseStore interface.

This module provides AsyncScyllaDBStore, which implements the same interface
as LangGraph's AsyncPostgresStore but uses ScyllaDB as the backend.
"""

import asyncio
import json
import logging
import time
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

from cassandra.cluster import Cluster, Session, ExecutionProfile, EXEC_PROFILE_DEFAULT, NoHostAvailable
from cassandra.policies import (
    WhiteListRoundRobinPolicy,
    DowngradingConsistencyRetryPolicy,
    TokenAwarePolicy,
    RoundRobinPolicy,
    DCAwareRoundRobinPolicy
)
from cassandra.query import SimpleStatement, BatchStatement, BatchType, ConsistencyLevel
from cassandra.concurrent import execute_concurrent_with_args
from cassandra.io.asyncioreactor import AsyncioConnection

# Import exceptions for proper error handling
try:
    from cassandra import (
        DriverException,
        RequestExecutionException,
        Unavailable,
        Timeout,
        ReadTimeout,
        WriteTimeout,
        CoordinationFailure,
        ReadFailure,
        WriteFailure,
        RequestValidationException,
        ConfigurationException,
        AlreadyExists,
        InvalidRequest,
        Unauthorized,
        AuthenticationFailed,
        OperationTimedOut,
        FunctionFailure,
    )
except ImportError:
    # Fallback if some exceptions are not available
    DriverException = Exception
    RequestExecutionException = Exception
    Unavailable = Exception
    Timeout = Exception
    ReadTimeout = Exception
    WriteTimeout = Exception
    CoordinationFailure = Exception
    ReadFailure = Exception
    WriteFailure = Exception
    RequestValidationException = Exception
    ConfigurationException = Exception
    AlreadyExists = Exception
    InvalidRequest = Exception
    Unauthorized = Exception
    AuthenticationFailed = Exception
    OperationTimedOut = Exception
    FunctionFailure = Exception

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


# Custom Exceptions - wrap Cassandra driver exceptions with context
class ScyllaDBStoreError(Exception):
    """
    Base exception for ScyllaDB store errors.

    Wraps underlying Cassandra driver exceptions with additional context
    and ensures proper logging.
    """

    def __init__(self, message: str, original_error: Exception | None = None):
        """
        Initialize store error.

        Args:
            message: Human-readable error message
            original_error: Original exception that caused this error
        """
        super().__init__(message)
        self.original_error = original_error
        self.message = message

        # Log error with full context
        if original_error:
            logger.error(
                f"ScyllaDBStoreError: {message}",
                exc_info=original_error,
                extra={
                    "error_type": type(original_error).__name__,
                    "error_message": str(original_error)
                }
            )
        else:
            logger.error(f"ScyllaDBStoreError: {message}")

    def __str__(self) -> str:
        """Return formatted error message."""
        if self.original_error:
            return f"{self.message} (caused by {type(self.original_error).__name__}: {self.original_error})"
        return self.message

    def __repr__(self) -> str:
        """Return detailed error representation."""
        return f"{self.__class__.__name__}(message={self.message!r}, original_error={self.original_error!r})"


class StoreConnectionError(ScyllaDBStoreError):
    """
    Raised when connection to cluster fails or no hosts are available.

    This is a fatal error that usually requires checking:
    - Network connectivity
    - ScyllaDB cluster status
    - Contact points configuration
    """

    def __init__(self, message: str = "Failed to connect to ScyllaDB cluster", original_error: Exception | None = None):
        super().__init__(message, original_error)


class StoreQueryError(ScyllaDBStoreError):
    """
    Raised when a query fails due to server-side issues.

    Includes coordination failures, read/write failures, and function failures.
    These may be retryable depending on the specific error.
    """

    def __init__(self, message: str, original_error: Exception | None = None, query: str | None = None):
        self.query = query
        if query:
            message = f"{message} [Query: {query[:100]}...]"
        super().__init__(message, original_error)


class StoreValidationError(ScyllaDBStoreError):
    """
    Raised when input validation fails.

    This includes:
    - Invalid namespaces or keys
    - Value size limits exceeded
    - Invalid query syntax
    - Configuration errors
    """

    def __init__(self, message: str, field: str | None = None, value: Any = None, original_error: Exception | None = None):
        self.field = field
        self.value = value
        if field:
            message = f"Validation error for '{field}': {message}"
        super().__init__(message, original_error)


class StoreTimeoutError(ScyllaDBStoreError):
    """
    Raised when an operation times out.

    Indicates that replicas failed to respond before the configured timeout.
    This is often a transient error that can be retried.
    """

    def __init__(
        self,
        message: str = "Operation timed out",
        original_error: Exception | None = None,
        timeout_seconds: float | None = None,
        operation_type: str | None = None
    ):
        self.timeout_seconds = timeout_seconds
        self.operation_type = operation_type

        details = []
        if operation_type:
            details.append(f"operation={operation_type}")
        if timeout_seconds:
            details.append(f"timeout={timeout_seconds}s")

        if details:
            message = f"{message} ({', '.join(details)})"

        super().__init__(message, original_error)


class StoreUnavailableError(ScyllaDBStoreError):
    """
    Raised when required replicas are unavailable.

    This means not enough live replicas exist to satisfy the consistency level.
    May indicate cluster health issues or misconfigured consistency requirements.
    """

    def __init__(
        self,
        message: str = "Required replicas unavailable",
        original_error: Exception | None = None,
        consistency_level: str | None = None,
        required_replicas: int | None = None,
        alive_replicas: int | None = None
    ):
        self.consistency_level = consistency_level
        self.required_replicas = required_replicas
        self.alive_replicas = alive_replicas

        details = []
        if consistency_level:
            details.append(f"consistency={consistency_level}")
        if required_replicas is not None and alive_replicas is not None:
            details.append(f"required={required_replicas}, alive={alive_replicas}")

        if details:
            message = f"{message} ({', '.join(details)})"

        super().__init__(message, original_error)


class StoreAuthenticationError(ScyllaDBStoreError):
    """
    Raised when authentication or authorization fails.

    This is a fatal error that requires checking credentials and permissions.
    """

    def __init__(
        self,
        message: str = "Authentication or authorization failed",
        original_error: Exception | None = None,
        username: str | None = None
    ):
        self.username = username
        if username:
            message = f"{message} for user '{username}'"
        super().__init__(message, original_error)


class ConnectionPoolDefaults:
    """
    Best practice defaults for ScyllaDB connection pooling.

    Based on ScyllaDB recommendations for production deployments.
    See: https://python-driver.docs.scylladb.com/stable/performance.html
    """

    # Connection pool sizes (per host)
    # For high-throughput applications: 2-4 connections per CPU core
    CORE_CONNECTIONS_PER_HOST = 2
    MAX_CONNECTIONS_PER_HOST = 8

    # Protocol and threading
    PROTOCOL_VERSION = 4  # CQL protocol version
    EXECUTOR_THREADS = 4  # Thread pool for async callbacks (2-4 recommended)

    # Timeouts (in seconds)
    CONNECT_TIMEOUT = 5.0  # Time to establish initial connection
    REQUEST_TIMEOUT = 10.0  # Default query timeout
    CONTROL_CONNECTION_TIMEOUT = 2.0  # Timeout for control connection operations
    IDLE_HEARTBEAT_TIMEOUT = 30.0  # Heartbeat interval for idle connections

    # Retry and reconnection
    MAX_SCHEMA_AGREEMENT_WAIT = 10  # Seconds to wait for schema agreement


class PoolConfig(TypedDict, total=False):
    """Configuration for connection pool and cluster settings."""
    # Connection pooling
    core_connections_per_host: int  # Minimum connections per host
    max_connections_per_host: int   # Maximum connections per host

    # Protocol and networking
    protocol_version: int  # CQL protocol version (default: 5)
    port: int             # Native transport port (default: 9042)

    # Performance tuning
    executor_threads: int  # Thread pool size for async callbacks (default: 2)

    # Load balancing policy
    load_balancing_policy: Any  # Policy for distributing requests

    # Retry and reconnection policies
    default_retry_policy: Any      # Retry policy for failed queries
    reconnection_policy: Any       # Policy for reconnecting to failed nodes

    # Timeouts
    connect_timeout: float         # Connection establishment timeout (seconds)
    control_connection_timeout: float  # Control connection timeout (seconds)
    idle_heartbeat_timeout: float  # Heartbeat timeout for idle connections


class TTLConfig(TypedDict, total=False):
    """Configuration for TTL behavior."""
    refresh_on_read: bool  # Refresh TTL on GET/SEARCH (default: True)
    default_ttl: float | None  # Default TTL in minutes
    sweep_interval_minutes: int | None  # Interval between sweeps


class ScyllaIndexConfig(TypedDict, total=False):
    """Configuration for secondary indexes and vector search."""
    enable_sai: bool  # Enable Storage-Attached Indexes
    indexed_fields: list[str]  # Fields to create secondary indexes on


class ExecutionProfileConfig(TypedDict, total=False):
    """Configuration for execution profiles."""
    consistency_level: Any  # Default consistency level for queries
    request_timeout: float  # Query timeout in seconds
    serial_consistency_level: Any  # Consistency for serial operations
    retry_policy: Any  # Retry policy for this profile


class NOT_PROVIDED:
    """Sentinel value for optional parameters."""
    pass


# Circuit Breaker for Resilience
class CircuitBreaker:
    """
    Circuit breaker pattern to prevent cascading failures.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, requests fail fast
    - HALF_OPEN: Testing if service recovered

    When failures exceed threshold, circuit opens and requests fail immediately
    without hitting the database. After timeout, circuit enters half-open state
    to test if service recovered.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout_seconds: float = 60.0
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            success_threshold: Number of successes needed to close circuit from half-open
            timeout_seconds: Time to wait before entering half-open state
        """
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout_seconds = timeout_seconds

        self._state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._lock = asyncio.Lock()

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Async function to execute
            *args, **kwargs: Arguments to pass to function

        Returns:
            Result from function

        Raises:
            StoreConnectionError: If circuit is open
        """
        async with self._lock:
            # Check if we should transition from OPEN to HALF_OPEN
            if self._state == "OPEN":
                if self._last_failure_time and (time.time() - self._last_failure_time) >= self.timeout_seconds:
                    logger.info("Circuit breaker entering HALF_OPEN state")
                    self._state = "HALF_OPEN"
                    self._success_count = 0
                else:
                    raise StoreConnectionError(
                        f"Circuit breaker is OPEN (too many failures). Retry after {self.timeout_seconds}s"
                    )

        # Execute function
        try:
            result = await func(*args, **kwargs)

            # Record success
            async with self._lock:
                if self._state == "HALF_OPEN":
                    self._success_count += 1
                    if self._success_count >= self.success_threshold:
                        logger.info("Circuit breaker closing (service recovered)")
                        self._state = "CLOSED"
                        self._failure_count = 0
                        self._success_count = 0
                elif self._state == "CLOSED":
                    # Reset failure count on success
                    self._failure_count = 0

            return result

        except Exception as e:
            # Record failure
            async with self._lock:
                self._failure_count += 1
                self._last_failure_time = time.time()

                if self._state == "HALF_OPEN":
                    logger.warning("Circuit breaker opening (service still unhealthy)")
                    self._state = "OPEN"
                    self._success_count = 0
                elif self._state == "CLOSED" and self._failure_count >= self.failure_threshold:
                    logger.error(f"Circuit breaker opening (failure threshold {self.failure_threshold} exceeded)")
                    self._state = "OPEN"

            raise

    def get_state(self) -> dict[str, Any]:
        """
        Get current circuit breaker state.

        Returns:
            Dictionary with current state, failure count, success count, and last failure time.
            Note: This is a non-blocking read of current state without lock for performance.
        """
        return {
            "state": self._state,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure_time": self._last_failure_time
        }

    async def reset(self):
        """Manually reset circuit breaker to CLOSED state."""
        async with self._lock:
            self._state = "CLOSED"
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
        logger.info("Circuit breaker manually reset to CLOSED")


# Metrics and Observability
class QueryMetrics:
    """
    Tracks query execution metrics for observability and performance monitoring.

    Maintains statistics on query latency, success/failure rates, and operation counts.
    Thread-safe and suitable for concurrent access.
    """

    def __init__(self):
        """Initialize metrics tracking."""
        self._lock = asyncio.Lock()
        self._query_count = 0
        self._error_count = 0
        self._total_latency_ms = 0.0
        self._min_latency_ms = float('inf')
        self._max_latency_ms = 0.0
        self._operation_counts: dict[str, int] = defaultdict(int)
        self._error_types: dict[str, int] = defaultdict(int)

        # Batch-specific metrics
        self._batch_count = 0
        self._batch_operation_count = 0
        self._atomic_batch_count = 0  # Batches using BatchStatement
        self._concurrent_batch_count = 0  # Mixed batches using concurrent execution
        self._logged_batch_count = 0
        self._unlogged_batch_count = 0
        self._batch_sizes: list[int] = []  # Track batch sizes for percentiles

    async def record_query(self, operation: str, latency_ms: float, success: bool = True, error_type: str | None = None):
        """
        Record a query execution.

        Args:
            operation: Operation type (e.g., 'get', 'put', 'search')
            latency_ms: Query latency in milliseconds
            success: Whether the query succeeded
            error_type: Type of error if query failed
        """
        async with self._lock:
            self._query_count += 1
            self._total_latency_ms += latency_ms
            self._min_latency_ms = min(self._min_latency_ms, latency_ms)
            self._max_latency_ms = max(self._max_latency_ms, latency_ms)
            self._operation_counts[operation] += 1

            if not success:
                self._error_count += 1
                if error_type:
                    self._error_types[error_type] += 1

            # Log slow queries (>100ms)
            if latency_ms > 100:
                logger.warning(
                    f"Slow query detected: {operation} took {latency_ms:.2f}ms",
                    extra={
                        "operation": operation,
                        "latency_ms": latency_ms,
                        "success": success
                    }
                )

    async def record_batch(
        self,
        batch_size: int,
        batch_type: str,  # "atomic_unlogged", "atomic_logged", "concurrent"
        latency_ms: float,
        success: bool = True
    ):
        """
        Record a batch operation execution.

        Args:
            batch_size: Number of operations in the batch
            batch_type: Type of batch execution
            latency_ms: Batch execution latency in milliseconds
            success: Whether the batch succeeded
        """
        async with self._lock:
            self._batch_count += 1
            self._batch_operation_count += batch_size
            self._batch_sizes.append(batch_size)

            if batch_type in ("atomic_unlogged", "atomic_logged"):
                self._atomic_batch_count += 1
                if batch_type == "atomic_unlogged":
                    self._unlogged_batch_count += 1
                else:
                    self._logged_batch_count += 1
            elif batch_type == "concurrent":
                self._concurrent_batch_count += 1

            # Also record in overall query metrics
            self._query_count += 1
            self._total_latency_ms += latency_ms
            self._min_latency_ms = min(self._min_latency_ms, latency_ms)
            self._max_latency_ms = max(self._max_latency_ms, latency_ms)
            self._operation_counts[f"batch_{batch_type}"] += 1

            if not success:
                self._error_count += 1

    async def get_stats(self) -> dict[str, Any]:
        """
        Get current metrics statistics.

        Returns:
            Dictionary containing all metrics
        """
        async with self._lock:
            avg_latency = self._total_latency_ms / self._query_count if self._query_count > 0 else 0
            error_rate = self._error_count / self._query_count if self._query_count > 0 else 0

            # Calculate batch statistics
            avg_batch_size = (
                self._batch_operation_count / self._batch_count
                if self._batch_count > 0 else 0
            )

            # Calculate batch size percentiles
            batch_p50 = batch_p95 = batch_p99 = 0
            if self._batch_sizes:
                sorted_sizes = sorted(self._batch_sizes)
                batch_p50 = sorted_sizes[int(0.50 * len(sorted_sizes))]
                batch_p95 = sorted_sizes[int(0.95 * len(sorted_sizes))]
                batch_p99 = sorted_sizes[int(0.99 * len(sorted_sizes))]

            return {
                "total_queries": self._query_count,
                "total_errors": self._error_count,
                "error_rate": error_rate,
                "avg_latency_ms": avg_latency,
                "min_latency_ms": self._min_latency_ms if self._min_latency_ms != float('inf') else 0,
                "max_latency_ms": self._max_latency_ms,
                "operations": dict(self._operation_counts),
                "error_types": dict(self._error_types),
                "batch_stats": {
                    "total_batches": self._batch_count,
                    "total_batch_operations": self._batch_operation_count,
                    "avg_batch_size": avg_batch_size,
                    "atomic_batches": self._atomic_batch_count,
                    "concurrent_batches": self._concurrent_batch_count,
                    "logged_batches": self._logged_batch_count,
                    "unlogged_batches": self._unlogged_batch_count,
                    "batch_size_p50": batch_p50,
                    "batch_size_p95": batch_p95,
                    "batch_size_p99": batch_p99,
                }
            }

    async def reset(self):
        """Reset all metrics counters."""
        async with self._lock:
            self._query_count = 0
            self._error_count = 0
            self._total_latency_ms = 0.0
            self._min_latency_ms = float('inf')
            self._max_latency_ms = 0.0
            self._operation_counts.clear()
            self._error_types.clear()

            # Reset batch metrics
            self._batch_count = 0
            self._batch_operation_count = 0
            self._atomic_batch_count = 0
            self._concurrent_batch_count = 0
            self._logged_batch_count = 0
            self._unlogged_batch_count = 0
            self._batch_sizes.clear()


# Validation Constants
class ValidationLimits:
    """Limits for validating inputs to prevent errors and ensure performance."""

    # ScyllaDB has a default max value size of ~1MB, but we set conservative limits
    MAX_VALUE_SIZE_BYTES = 1_000_000  # 1MB
    MAX_KEY_LENGTH = 1024  # 1KB for keys
    MAX_NAMESPACE_DEPTH = 10  # Reasonable depth for hierarchical namespaces
    MAX_NAMESPACE_LABEL_LENGTH = 256  # Max length per namespace label
    MAX_BATCH_SIZE = 100  # Maximum operations in a single batch

    # Warnings for performance
    WARN_VALUE_SIZE_BYTES = 100_000  # Warn if value > 100KB
    WARN_BATCH_SIZE = 50  # Warn if batch > 50 operations


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
        execution_profiles: dict[str, ExecutionProfileConfig] | None = None,
    ) -> None:
        """
        Initialize ScyllaDB store.

        Args:
            session: ScyllaDB/Cassandra session
            keyspace: Keyspace name
            deserializer: Optional JSON deserializer
            index: Optional index configuration
            ttl: Optional TTL configuration
            execution_profiles: Optional execution profile configurations
        """
        self.session = session
        self.keyspace = keyspace
        self.deserializer = deserializer or json.loads
        self.index_config = index or {}
        self.ttl_config = ttl or {}
        self.execution_profiles = execution_profiles or {}
        self.lock = asyncio.Lock()
        self._ttl_sweeper_task: asyncio.Task | None = None
        self._ttl_stop_event = asyncio.Event()

        # Prepared statements cache - will be populated during setup()
        self._prepared_statements: dict[str, Any] = {}

        # Metrics tracking
        self.metrics = QueryMetrics()

        # Circuit breaker for resilience (optional, disabled by default)
        self.circuit_breaker: CircuitBreaker | None = None

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
        execution_profiles: dict[str, ExecutionProfileConfig] | None = None,
    ) -> AsyncIterator["AsyncScyllaDBStore"]:
        """
        Create AsyncScyllaDBStore from contact points.

        Args:
            contact_points: List of ScyllaDB node addresses
            keyspace: Keyspace name
            pool_config: Optional connection pool configuration
            index: Optional index configuration
            ttl: Optional TTL configuration
            execution_profiles: Optional execution profile configurations

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

        # Apply best practice defaults if not specified
        cluster_config = {
            "contact_points": contact_points,
            "connection_class": AsyncioConnection,

            # Connection pool optimization
            "protocol_version": pool_config.get("protocol_version", ConnectionPoolDefaults.PROTOCOL_VERSION),
            "port": pool_config.get("port", 9042),
            "executor_threads": pool_config.get("executor_threads", ConnectionPoolDefaults.EXECUTOR_THREADS),

            # Timeouts
            "connect_timeout": pool_config.get("connect_timeout", ConnectionPoolDefaults.CONNECT_TIMEOUT),
            "control_connection_timeout": pool_config.get("control_connection_timeout", ConnectionPoolDefaults.CONTROL_CONNECTION_TIMEOUT),
            "idle_heartbeat_timeout": pool_config.get("idle_heartbeat_timeout", ConnectionPoolDefaults.IDLE_HEARTBEAT_TIMEOUT),
            "max_schema_agreement_wait": pool_config.get("max_schema_agreement_wait", ConnectionPoolDefaults.MAX_SCHEMA_AGREEMENT_WAIT),

            # Compression for network efficiency (lz4 is fastest)
            "compression": pool_config.get("compression", True),  # Enables lz4 compression
        }

        # Add optional configurations if provided
        if "load_balancing_policy" in pool_config:
            cluster_config["load_balancing_policy"] = pool_config["load_balancing_policy"]
        else:
            # Use ScyllaDB-optimized TokenAwarePolicy for shard awareness
            # This reduces latency by routing requests directly to the right shard
            cluster_config["load_balancing_policy"] = TokenAwarePolicy(DCAwareRoundRobinPolicy())
            logger.info("Using TokenAwarePolicy for ScyllaDB shard-aware routing")

        # Enable shard awareness for ScyllaDB (enabled by default, but make it explicit)
        if "shard_aware_options" not in pool_config:
            cluster_config["shard_aware_options"] = {"disable": False}

        if "default_retry_policy" in pool_config:
            cluster_config["default_retry_policy"] = pool_config["default_retry_policy"]

        if "reconnection_policy" in pool_config:
            cluster_config["reconnection_policy"] = pool_config["reconnection_policy"]

        if "core_connections_per_host" in pool_config:
            cluster_config["core_connections_per_host"] = pool_config["core_connections_per_host"]
        else:
            # Apply defaults
            cluster_config["core_connections_per_host"] = {
                "EXEC_PROFILE_DEFAULT": ConnectionPoolDefaults.CORE_CONNECTIONS_PER_HOST
            }

        if "max_connections_per_host" in pool_config:
            cluster_config["max_connections_per_host"] = pool_config["max_connections_per_host"]
        else:
            # Apply defaults
            cluster_config["max_connections_per_host"] = {
                "EXEC_PROFILE_DEFAULT": ConnectionPoolDefaults.MAX_CONNECTIONS_PER_HOST
            }

        # Create cluster with optimized configuration
        cluster = Cluster(**cluster_config)

        logger.info(
            f"Created cluster with optimized settings: "
            f"protocol_version={cluster_config['protocol_version']}, "
            f"executor_threads={cluster_config['executor_threads']}, "
            f"compression={cluster_config['compression']}"
        )

        # Add execution profiles if configured
        if execution_profiles:
            for profile_name, profile_config in execution_profiles.items():
                profile = ExecutionProfile(**profile_config)
                cluster.add_execution_profile(profile_name, profile)
                logger.info(f"Added execution profile: {profile_name}")

        store = None
        try:
            # Connect to cluster - with AsyncioConnection this should be async-friendly
            # but connect() is still synchronous, so we run it in executor once at startup
            session = await asyncio.get_running_loop().run_in_executor(
                None, cluster.connect
            )

            store = cls(
                session=session,
                keyspace=keyspace,
                index=index,
                ttl=ttl,
                execution_profiles=execution_profiles,
            )

            yield store

        finally:
            # Cleanup
            if store is not None and hasattr(store, '_ttl_sweeper_task') and store._ttl_sweeper_task:
                await store.stop_ttl_sweeper()

            # Shutdown cluster - still synchronous, run in executor once at cleanup
            await asyncio.get_running_loop().run_in_executor(None, cluster.shutdown)

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

        # Prepare all statements once during setup for better performance
        await self._prepare_statements()

        logger.info(f"ScyllaDB store setup complete in keyspace '{self.keyspace}'")

    def enable_circuit_breaker(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout_seconds: float = 60.0
    ):
        """
        Enable circuit breaker for resilience against cascading failures.

        Args:
            failure_threshold: Number of failures before opening circuit (default: 5)
            success_threshold: Number of successes needed to close circuit (default: 2)
            timeout_seconds: Time to wait before testing recovery (default: 60.0)

        Example:
            ```python
            await store.setup()
            store.enable_circuit_breaker(failure_threshold=3, timeout_seconds=30.0)
            ```
        """
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            success_threshold=success_threshold,
            timeout_seconds=timeout_seconds
        )
        logger.info(
            f"Circuit breaker enabled: failure_threshold={failure_threshold}, "
            f"timeout={timeout_seconds}s"
        )

    async def warmup_connections(self, num_queries: int = 10):
        """
        Warm up connections to all cluster nodes.

        Executes simple queries to pre-establish connections and prepare
        the connection pool. Improves first-request latency.

        Args:
            num_queries: Number of warmup queries to execute (default: 10)

        Example:
            ```python
            await store.setup()
            await store.warmup_connections()
            logger.info("Connections warmed up, ready for traffic")
            ```
        """
        logger.info(f"Warming up connections with {num_queries} queries...")
        start_time = time.perf_counter()

        try:
            # Execute simple queries concurrently to warm up connections
            warmup_tasks = []
            for i in range(num_queries):
                task = self._execute_async("SELECT now() FROM system.local", None)
                warmup_tasks.append(task)

            results = await asyncio.gather(*warmup_tasks, return_exceptions=True)

            # Count successes
            successes = sum(1 for r in results if not isinstance(r, Exception))
            failures = num_queries - successes

            latency_ms = (time.perf_counter() - start_time) * 1000

            logger.info(
                f"Connection warmup complete: {successes}/{num_queries} successful "
                f"in {latency_ms:.2f}ms"
            )

            if failures > 0:
                logger.warning(f"Connection warmup had {failures} failures")

        except Exception as e:
            logger.error(f"Connection warmup failed: {e}", exc_info=e)
            raise StoreConnectionError("Connection warmup failed", original_error=e)

    def add_execution_profile(
        self,
        name: str,
        *,
        consistency_level: Any = None,
        request_timeout: float | None = None,
        serial_consistency_level: Any = None,
        retry_policy: Any = None,
        load_balancing_policy: Any = None,
        row_factory: Any = None,
    ) -> None:
        """
        Add a custom execution profile to the session.

        Execution profiles allow different queries to use different settings
        for consistency, timeout, retry policies, etc.

        Args:
            name: Profile name to reference in queries
            consistency_level: Default consistency level (e.g., ConsistencyLevel.QUORUM)
            request_timeout: Query timeout in seconds
            serial_consistency_level: Consistency for LWT operations
            retry_policy: Retry policy instance
            load_balancing_policy: Load balancing policy instance
            row_factory: Function to create row objects from results

        Example:
            ```python
            from cassandra.query import ConsistencyLevel
            from cassandra.policies import RetryPolicy

            # Add a profile for critical reads
            store.add_execution_profile(
                'critical_reads',
                consistency_level=ConsistencyLevel.QUORUM,
                request_timeout=30.0
            )

            # Add a profile for fast writes
            store.add_execution_profile(
                'fast_writes',
                consistency_level=ConsistencyLevel.ONE,
                request_timeout=5.0
            )
            ```
        """
        from cassandra.cluster import ExecutionProfile

        profile_kwargs = {}
        if consistency_level is not None:
            profile_kwargs['consistency_level'] = consistency_level
        if request_timeout is not None:
            profile_kwargs['request_timeout'] = request_timeout
        if serial_consistency_level is not None:
            profile_kwargs['serial_consistency_level'] = serial_consistency_level
        if retry_policy is not None:
            profile_kwargs['retry_policy'] = retry_policy
        if load_balancing_policy is not None:
            profile_kwargs['load_balancing_policy'] = load_balancing_policy
        if row_factory is not None:
            profile_kwargs['row_factory'] = row_factory

        profile = ExecutionProfile(**profile_kwargs)

        # Check if profile already exists
        if name in self.session.cluster.profile_manager.profiles:
            logger.warning(f"Execution profile '{name}' already exists, skipping")
            return

        self.session.cluster.add_execution_profile(name, profile)
        logger.info(f"Added execution profile '{name}' with settings: {profile_kwargs}")

    def create_standard_profiles(self) -> None:
        """
        Create standard execution profiles for common use cases.

        Profiles created:
        - 'strong_reads': QUORUM consistency, 30s timeout (for critical reads)
        - 'fast_writes': ONE consistency, 5s timeout (for high-throughput writes)
        - 'lwt_operations': SERIAL consistency for LWT (for atomic operations)
        - 'analytics': ALL consistency, 60s timeout (for analytics queries)

        Example:
            ```python
            await store.setup()
            store.create_standard_profiles()

            # Use in queries via _execute_async with execution_profile parameter
            ```

        Note:
            Currently execution profiles are configured but not actively used
            in query execution. This is a foundation for future enhancement.
        """
        from cassandra.query import ConsistencyLevel

        # Strong reads for critical data
        self.add_execution_profile(
            'strong_reads',
            consistency_level=ConsistencyLevel.QUORUM,
            request_timeout=30.0
        )

        # Fast writes for high throughput
        self.add_execution_profile(
            'fast_writes',
            consistency_level=ConsistencyLevel.ONE,
            request_timeout=5.0
        )

        # LWT operations need SERIAL consistency
        self.add_execution_profile(
            'lwt_operations',
            consistency_level=ConsistencyLevel.QUORUM,
            serial_consistency_level=ConsistencyLevel.SERIAL,
            request_timeout=30.0
        )

        # Analytics queries with higher timeout
        self.add_execution_profile(
            'analytics',
            consistency_level=ConsistencyLevel.ALL,
            request_timeout=60.0
        )

        logger.info("Created 4 standard execution profiles: strong_reads, fast_writes, lwt_operations, analytics")

    def get_shard_awareness_info(self) -> dict[str, Any]:
        """
        Get ScyllaDB shard awareness information and statistics.

        Returns detailed information about shard-aware connections, which
        optimize performance by routing queries directly to the correct shard.

        Returns:
            Dictionary containing:
            - is_shard_aware: Whether cluster supports shard awareness
            - shard_stats: Connection status for all shards (if available)
            - cluster_metadata: Additional cluster information

        Example:
            ```python
            await store.setup()
            info = store.get_shard_awareness_info()

            if info['is_shard_aware']:
                print(f"✓ Shard awareness enabled")
                print(f"Shard stats: {info['shard_stats']}")
            else:
                print("✗ Shard awareness not available")
            ```

        Note:
            Shard awareness is a ScyllaDB-specific optimization that reduces
            latency by eliminating inter-shard communication. It's enabled
            automatically when using TokenAwarePolicy.
        """
        cluster = self.session.cluster

        # Extract contact points - they may be strings or host objects
        contact_points = []
        for cp in cluster.contact_points:
            if isinstance(cp, str):
                contact_points.append(cp)
            elif hasattr(cp, 'address'):
                contact_points.append(cp.address)
            else:
                contact_points.append(str(cp))

        result = {
            "is_shard_aware": False,
            "shard_stats": None,
            "cluster_metadata": {
                "contact_points": contact_points,
                "protocol_version": cluster.protocol_version,
                "compression": cluster.compression,
            }
        }

        # Check if cluster supports shard awareness
        try:
            if hasattr(cluster, 'is_shard_aware'):
                result["is_shard_aware"] = cluster.is_shard_aware()

                # Get shard statistics if available
                if result["is_shard_aware"] and hasattr(cluster, 'shard_aware_stats'):
                    result["shard_stats"] = cluster.shard_aware_stats()
        except Exception as e:
            logger.warning(f"Could not retrieve shard awareness info: {e}")

        return result

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

        Raises:
            StoreValidationError: If inputs are invalid
            StoreQueryError: If read fails
            StoreTimeoutError: If operation times out
            StoreUnavailableError: If required replicas are unavailable
        """
        # Validate inputs
        self._validate_namespace(namespace)
        self._validate_key(key)

        prefix = self._namespace_to_prefix(namespace)
        should_refresh = refresh_ttl if refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

        # Use pre-prepared statement
        result = await self._execute_prepared("get", (prefix, key))

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

        Raises:
            StoreValidationError: If inputs are invalid
            StoreQueryError: If write fails
            StoreTimeoutError: If operation times out
            StoreUnavailableError: If required replicas are unavailable
        """
        # Validate all inputs
        self._validate_namespace(namespace)
        self._validate_key(key)
        self._validate_value(value)

        prefix = self._namespace_to_prefix(namespace)
        value_json = json.dumps(value)
        now = datetime.now(timezone.utc)

        # Determine TTL
        # Note: ttl parameter is in seconds, but we store ttl_minutes for compatibility
        ttl_minutes = None
        ttl_seconds = None

        if ttl is not NOT_PROVIDED:
            if ttl is not None:
                ttl_seconds = int(ttl)  # ttl is already in seconds
                ttl_minutes = int(ttl / 60)  # convert to minutes for storage
        elif self.ttl_config.get("default_ttl"):
            default_ttl_seconds = self.ttl_config["default_ttl"]
            ttl_seconds = int(default_ttl_seconds)
            ttl_minutes = int(default_ttl_seconds / 60)

        # Use pre-prepared statement
        if ttl_seconds:
            await self._execute_prepared(
                "put_with_ttl",
                (prefix, key, value_json, now, now, ttl_minutes, ttl_seconds)
            )
        else:
            await self._execute_prepared(
                "put_no_ttl",
                (prefix, key, value_json, now, now, ttl_minutes)
            )

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
        await self._execute_prepared("delete", (prefix, key))

    # Lightweight Transaction (LWT) Methods

    async def aput_if_not_exists(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        *,
        ttl: float | None = None,
    ) -> bool:
        """
        Insert item only if it doesn't already exist (atomic operation).

        Uses Lightweight Transactions (LWT) for atomic conditional insert.
        This prevents race conditions when multiple clients try to create
        the same key simultaneously.

        Args:
            namespace: Hierarchical path
            key: Item identifier
            value: Data to store
            ttl: Optional time-to-live in seconds

        Returns:
            True if insert succeeded, False if key already exists

        Example:
            ```python
            # Try to claim a lock
            success = await store.aput_if_not_exists(
                namespace=("locks",),
                key="resource_123",
                value={"owner": "worker_1", "acquired_at": time.time()},
                ttl=60.0
            )
            if success:
                print("Lock acquired!")
            else:
                print("Lock already held by another worker")
            ```

        Note:
            LWT operations use SERIAL consistency level and are slower than
            regular writes. Use only when atomicity is required.
        """
        self._validate_namespace(namespace)
        self._validate_key(key)
        self._validate_value(value)

        prefix = self._namespace_to_prefix(namespace)
        value_json = json.dumps(value)
        now = datetime.now(timezone.utc)

        # Determine TTL
        ttl_seconds = None
        ttl_minutes = None
        if ttl is not None:
            ttl_seconds = int(ttl)
            ttl_minutes = int(ttl / 60)

        # Execute LWT
        if ttl_seconds:
            result = await self._execute_prepared(
                "put_if_not_exists_with_ttl",
                (prefix, key, value_json, now, now, ttl_minutes, ttl_seconds)
            )
        else:
            result = await self._execute_prepared(
                "put_if_not_exists_no_ttl",
                (prefix, key, value_json, now, now, ttl_minutes)
            )

        # Check [applied] column
        return result[0].applied if result else False

    async def aupdate_if_exists(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        *,
        ttl: float | None = None,
    ) -> bool:
        """
        Update item only if it already exists (atomic operation).

        Uses Lightweight Transactions (LWT) to ensure the key exists
        before updating. Prevents accidental creation of new keys.

        Args:
            namespace: Hierarchical path
            key: Item identifier
            value: New data to store
            ttl: Optional time-to-live in seconds

        Returns:
            True if update succeeded, False if key doesn't exist

        Example:
            ```python
            # Update only if user exists
            success = await store.aupdate_if_exists(
                namespace=("users", "123"),
                key="profile",
                value={"name": "Alice", "age": 31}
            )
            if not success:
                print("User not found, cannot update")
            ```
        """
        self._validate_namespace(namespace)
        self._validate_key(key)
        self._validate_value(value)

        prefix = self._namespace_to_prefix(namespace)
        value_json = json.dumps(value)
        now = datetime.now(timezone.utc)

        # Execute LWT
        if ttl is not None:
            ttl_seconds = int(ttl)
            result = await self._execute_prepared(
                "update_if_exists_with_ttl",
                (ttl_seconds, value_json, now, prefix, key)
            )
        else:
            result = await self._execute_prepared(
                "update_if_exists_no_ttl",
                (value_json, now, prefix, key)
            )

        return result[0].applied if result else False

    async def acompare_and_set(
        self,
        namespace: tuple[str, ...],
        key: str,
        expected_value: dict[str, Any],
        new_value: dict[str, Any],
        *,
        ttl: float | None = None,
    ) -> bool:
        """
        Update item only if current value matches expected value (CAS operation).

        Uses Lightweight Transactions (LWT) for atomic compare-and-set.
        This is the classic optimistic locking pattern.

        Args:
            namespace: Hierarchical path
            key: Item identifier
            expected_value: Value that must match current stored value
            new_value: New value to set if comparison succeeds
            ttl: Optional time-to-live in seconds

        Returns:
            True if CAS succeeded, False if current value doesn't match expected

        Example:
            ```python
            # Increment counter atomically
            while True:
                item = await store.aget(("counters",), "page_views")
                current = item.value if item else {"count": 0}
                new = {"count": current["count"] + 1}

                if await store.acompare_and_set(
                    namespace=("counters",),
                    key="page_views",
                    expected_value=current,
                    new_value=new
                ):
                    break  # Success
                # Retry if another client modified it
            ```

        Note:
            For high-contention scenarios, consider using ScyllaDB counters
            instead of CAS loops.
        """
        self._validate_namespace(namespace)
        self._validate_key(key)
        self._validate_value(expected_value)
        self._validate_value(new_value)

        prefix = self._namespace_to_prefix(namespace)
        expected_json = json.dumps(expected_value)
        new_json = json.dumps(new_value)
        now = datetime.now(timezone.utc)

        # Execute CAS
        if ttl is not None:
            ttl_seconds = int(ttl)
            result = await self._execute_prepared(
                "cas_update_with_ttl",
                (ttl_seconds, new_json, now, prefix, key, expected_json)
            )
        else:
            result = await self._execute_prepared(
                "cas_update_no_ttl",
                (new_json, now, prefix, key, expected_json)
            )

        return result[0].applied if result else False

    async def adelete_if_exists(
        self,
        namespace: tuple[str, ...],
        key: str,
    ) -> bool:
        """
        Delete item only if it exists (atomic operation).

        Uses Lightweight Transactions (LWT) to ensure the key exists
        before deleting.

        Args:
            namespace: Hierarchical path
            key: Item identifier

        Returns:
            True if delete succeeded, False if key doesn't exist
        """
        self._validate_namespace(namespace)
        self._validate_key(key)

        prefix = self._namespace_to_prefix(namespace)
        result = await self._execute_prepared("delete_if_exists", (prefix, key))

        return result[0].applied if result else False

    async def adelete_if_value(
        self,
        namespace: tuple[str, ...],
        key: str,
        expected_value: dict[str, Any],
    ) -> bool:
        """
        Delete item only if current value matches expected value (atomic operation).

        Uses Lightweight Transactions (LWT) for conditional delete.

        Args:
            namespace: Hierarchical path
            key: Item identifier
            expected_value: Value that must match current stored value

        Returns:
            True if delete succeeded, False if value doesn't match

        Example:
            ```python
            # Release lock only if we own it
            success = await store.adelete_if_value(
                namespace=("locks",),
                key="resource_123",
                expected_value={"owner": "worker_1"}
            )
            if not success:
                print("Cannot release lock - owned by another worker")
            ```
        """
        self._validate_namespace(namespace)
        self._validate_key(key)
        self._validate_value(expected_value)

        prefix = self._namespace_to_prefix(namespace)
        expected_json = json.dumps(expected_value)
        result = await self._execute_prepared(
            "delete_if_value",
            (prefix, key, expected_json)
        )

        return result[0].applied if result else False

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
        fetch_size: int | None = None,
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
            fetch_size: Page size for query paging (default: 5000)
                       Larger values = fewer round trips but more memory
                       Smaller values = more round trips but less memory

        Returns:
            List of SearchItem objects

        Note:
            Uses efficient token-range query to scan partitions matching prefix.
            Filters are applied in Python after fetching. For complex filtering,
            consider using ScyllaDB's SAI indexes.

            Query paging is automatically handled by the driver with the specified
            fetch_size. This prevents memory issues when scanning large tables.
        """
        self._validate_namespace(namespace_prefix)

        prefix = self._namespace_to_prefix(namespace_prefix)
        should_refresh = refresh_ttl if refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

        # Use token-range query for efficient full table scan
        # This avoids ALLOW FILTERING by scanning all partitions efficiently
        # Set fetch_size for automatic paging (default: 5000 rows per page)
        from cassandra.query import SimpleStatement

        query_str = "SELECT * FROM store"
        statement = SimpleStatement(query_str, fetch_size=fetch_size or 5000)

        # Execute with automatic paging
        results = await self._execute_async(statement, None)

        if not results:
            return []

        # Filter by prefix and apply custom filters in Python
        items = []
        for row in results:
            # Check if prefix matches (startswith for hierarchical matching)
            if not row.prefix.startswith(prefix):
                continue

            value = json.loads(row.value)

            # Apply custom filters
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

    async def abatch(
        self,
        ops: Iterable[Op],
        *,
        batch_type: str = "UNLOGGED",
        max_retries: int = 0,
        retry_delay: float = 0.1,
        retry_backoff: float = 2.0
    ) -> list[Result]:
        """
        Execute multiple operations in a batch.

        Intelligently uses atomic BatchStatement when all operations are PutOps,
        otherwise uses concurrent execution for mixed operation types.

        Args:
            ops: Iterable of operations (GetOp, PutOp, SearchOp, ListNamespacesOp)
            batch_type: For all-PUT batches - "LOGGED" (atomic, slower) or "UNLOGGED" (faster, default)
            max_retries: Maximum number of retry attempts (0 = no retries)
            retry_delay: Initial delay between retries in seconds
            retry_backoff: Multiplier for delay after each retry (exponential backoff)

        Returns:
            List of results corresponding to each operation

        Raises:
            StoreValidationError: If batch size exceeds limits
            StoreQueryError: If batch execution fails after all retries
            StoreTimeoutError: If operations time out
            StoreUnavailableError: If required replicas unavailable

        Note:
            - All PutOps: Uses atomic BatchStatement with specified batch_type
              - LOGGED: Full atomicity across partitions (performance cost)
              - UNLOGGED: Better performance, atomic within partition only
            - Mixed operations: Uses concurrent execution (batch_type ignored)
            - Retries: Only retryable errors (timeout, unavailable) trigger retries
        """
        ops_list = list(ops)

        # Validate batch size
        if len(ops_list) > ValidationLimits.MAX_BATCH_SIZE:
            raise StoreValidationError(
                f"Batch size ({len(ops_list)}) exceeds maximum ({ValidationLimits.MAX_BATCH_SIZE})",
                field="batch_size",
                value=len(ops_list)
            )

        # Warn for large batches
        if len(ops_list) > ValidationLimits.WARN_BATCH_SIZE:
            logger.warning(
                f"Large batch detected ({len(ops_list)} operations). "
                f"Consider breaking into smaller batches for better performance."
            )

        # Retry loop with exponential backoff
        last_exception = None
        current_delay = retry_delay

        for attempt in range(max_retries + 1):
            try:
                results: list[Result] = [None] * len(ops_list)

                # Check if all operations are PutOps - if so, use atomic BatchStatement
                all_puts = all(isinstance(op, PutOp) for op in ops_list)

                # Measure batch execution time
                import time
                start_time = time.perf_counter()

                return await self._execute_batch_internal(
                    ops_list, results, all_puts, batch_type, start_time
                )

            except (StoreTimeoutError, StoreUnavailableError) as e:
                last_exception = e

                # Check if we should retry
                if attempt < max_retries:
                    logger.warning(
                        f"Batch operation failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                        f"Retrying in {current_delay:.2f}s..."
                    )
                    await asyncio.sleep(current_delay)
                    current_delay *= retry_backoff
                else:
                    logger.error(
                        f"Batch operation failed after {max_retries + 1} attempts: {e}"
                    )
                    raise

            except Exception as e:
                # Non-retryable error
                logger.error(f"Batch operation failed with non-retryable error: {e}")
                raise

        # Should not reach here, but just in case
        if last_exception:
            raise last_exception
        raise StoreQueryError("Batch execution failed unexpectedly")

    async def _execute_batch_internal(
        self,
        ops_list: list[Op],
        results: list[Result],
        all_puts: bool,
        batch_type: str,
        start_time: float
    ) -> list[Result]:
        """
        Internal method to execute batch operations.

        Separated for retry logic to work correctly.
        """
        import time

        if all_puts and ops_list:
            # Use true atomic batch for all-PUT operations
            try:
                await self._batch_put_atomic(ops_list, results, batch_type=batch_type)

                # Record metrics
                latency_ms = (time.perf_counter() - start_time) * 1000
                metric_type = f"atomic_{batch_type.lower()}"
                await self.metrics.record_batch(
                    batch_size=len(ops_list),
                    batch_type=metric_type,
                    latency_ms=latency_ms,
                    success=True
                )

                return results
            except Exception as e:
                # Record failure
                latency_ms = (time.perf_counter() - start_time) * 1000
                metric_type = f"atomic_{batch_type.lower()}"
                await self.metrics.record_batch(
                    batch_size=len(ops_list),
                    batch_type=metric_type,
                    latency_ms=latency_ms,
                    success=False
                )
                raise

        # Mixed operations - use concurrent execution
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
        try:
            await asyncio.gather(*tasks)

            # Record metrics for concurrent batch
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_batch(
                batch_size=len(ops_list),
                batch_type="concurrent",
                latency_ms=latency_ms,
                success=True
            )
        except Exception as e:
            # Record failure
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_batch(
                batch_size=len(ops_list),
                batch_type="concurrent",
                latency_ms=latency_ms,
                success=False
            )
            raise

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

    async def get_metrics(self) -> dict[str, Any]:
        """
        Get current performance metrics.

        Returns:
            Dictionary containing query metrics including:
            - total_queries: Total number of queries executed
            - total_errors: Total number of errors
            - error_rate: Percentage of queries that failed
            - avg_latency_ms: Average query latency in milliseconds
            - min_latency_ms: Minimum query latency
            - max_latency_ms: Maximum query latency
            - operations: Count of each operation type
            - error_types: Count of each error type

        Example:
            ```python
            metrics = await store.get_metrics()
            print(f"Average latency: {metrics['avg_latency_ms']:.2f}ms")
            print(f"Error rate: {metrics['error_rate']*100:.2f}%")
            ```
        """
        return await self.metrics.get_stats()

    async def reset_metrics(self):
        """
        Reset all performance metrics counters.

        Useful for starting fresh measurements or after deployment.
        """
        await self.metrics.reset()
        logger.info("Performance metrics reset")

    async def health_check(self) -> dict[str, Any]:
        """
        Perform comprehensive health check of the store.

        Checks:
        - Database connectivity
        - Keyspace accessibility
        - Basic query execution
        - Prepared statements availability

        Returns:
            Dictionary containing health status:
            - status: "healthy" | "degraded" | "unhealthy"
            - timestamp: Current timestamp
            - checks: Individual check results
            - metrics: Current performance metrics
            - latency_ms: Health check execution time

        Example:
            ```python
            health = await store.health_check()
            if health['status'] != 'healthy':
                logger.error(f"Store unhealthy: {health}")
            ```
        """
        start_time = time.perf_counter()
        checks = {}
        overall_status = "healthy"

        try:
            # Check 1: Database connectivity
            try:
                await self._execute_async("SELECT now() FROM system.local", None)
                checks["connectivity"] = {"status": "healthy", "message": "Connected to cluster"}
            except Exception as e:
                checks["connectivity"] = {"status": "unhealthy", "message": f"Connection failed: {e}"}
                overall_status = "unhealthy"

            # Check 2: Keyspace accessibility
            try:
                await self._execute_async(f"SELECT * FROM {self.keyspace}.store LIMIT 1", None)
                checks["keyspace"] = {"status": "healthy", "message": f"Keyspace '{self.keyspace}' accessible"}
            except Exception as e:
                checks["keyspace"] = {"status": "degraded", "message": f"Keyspace access issue: {e}"}
                if overall_status == "healthy":
                    overall_status = "degraded"

            # Check 3: Prepared statements
            if self._prepared_statements:
                checks["prepared_statements"] = {
                    "status": "healthy",
                    "count": len(self._prepared_statements),
                    "message": f"{len(self._prepared_statements)} statements prepared"
                }
            else:
                checks["prepared_statements"] = {
                    "status": "degraded",
                    "count": 0,
                    "message": "No prepared statements (setup() may not have been called)"
                }
                if overall_status == "healthy":
                    overall_status = "degraded"

            # Check 4: Get current metrics
            metrics = await self.metrics.get_stats()
            checks["metrics"] = {
                "status": "healthy" if metrics["error_rate"] < 0.05 else "degraded",
                "error_rate": metrics["error_rate"],
                "avg_latency_ms": metrics["avg_latency_ms"],
                "message": f"Error rate: {metrics['error_rate']*100:.2f}%, Avg latency: {metrics['avg_latency_ms']:.2f}ms"
            }

            if metrics["error_rate"] >= 0.05:  # 5% error threshold
                if overall_status == "healthy":
                    overall_status = "degraded"

        except Exception as e:
            logger.error(f"Health check failed: {e}", exc_info=e)
            checks["overall"] = {"status": "unhealthy", "message": f"Health check error: {e}"}
            overall_status = "unhealthy"

        latency_ms = (time.perf_counter() - start_time) * 1000

        return {
            "status": overall_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "latency_ms": latency_ms,
            "checks": checks,
            "keyspace": self.keyspace,
            "prepared_statements_count": len(self._prepared_statements)
        }

    async def export_prometheus_metrics(self) -> str:
        """
        Export metrics in Prometheus text format.

        Returns metrics in the standard Prometheus exposition format suitable
        for scraping by Prometheus server or pushing to Pushgateway.

        Returns:
            String containing metrics in Prometheus format

        Example:
            ```python
            # Expose via HTTP endpoint
            from aiohttp import web

            async def metrics_handler(request):
                metrics = await store.export_prometheus_metrics()
                return web.Response(text=metrics, content_type='text/plain')

            app.router.add_get('/metrics', metrics_handler)
            ```
        """
        metrics = await self.metrics.get_stats()

        # Build Prometheus metrics
        lines = []

        # Total queries
        lines.append("# HELP scylladb_store_queries_total Total number of queries executed")
        lines.append("# TYPE scylladb_store_queries_total counter")
        lines.append(f"scylladb_store_queries_total {metrics['total_queries']}")
        lines.append("")

        # Total errors
        lines.append("# HELP scylladb_store_errors_total Total number of errors")
        lines.append("# TYPE scylladb_store_errors_total counter")
        lines.append(f"scylladb_store_errors_total {metrics['total_errors']}")
        lines.append("")

        # Error rate
        lines.append("# HELP scylladb_store_error_rate Current error rate (0.0-1.0)")
        lines.append("# TYPE scylladb_store_error_rate gauge")
        lines.append(f"scylladb_store_error_rate {metrics['error_rate']:.6f}")
        lines.append("")

        # Average latency
        lines.append("# HELP scylladb_store_latency_avg_ms Average query latency in milliseconds")
        lines.append("# TYPE scylladb_store_latency_avg_ms gauge")
        lines.append(f"scylladb_store_latency_avg_ms {metrics['avg_latency_ms']:.2f}")
        lines.append("")

        # Min latency
        lines.append("# HELP scylladb_store_latency_min_ms Minimum query latency in milliseconds")
        lines.append("# TYPE scylladb_store_latency_min_ms gauge")
        lines.append(f"scylladb_store_latency_min_ms {metrics['min_latency_ms']:.2f}")
        lines.append("")

        # Max latency
        lines.append("# HELP scylladb_store_latency_max_ms Maximum query latency in milliseconds")
        lines.append("# TYPE scylladb_store_latency_max_ms gauge")
        lines.append(f"scylladb_store_latency_max_ms {metrics['max_latency_ms']:.2f}")
        lines.append("")

        # Operations by type
        lines.append("# HELP scylladb_store_operations_total Total operations by type")
        lines.append("# TYPE scylladb_store_operations_total counter")
        for operation, count in metrics['operations'].items():
            lines.append(f'scylladb_store_operations_total{{operation="{operation}"}} {count}')
        lines.append("")

        # Errors by type
        lines.append("# HELP scylladb_store_errors_by_type_total Total errors by type")
        lines.append("# TYPE scylladb_store_errors_by_type_total counter")
        for error_type, count in metrics['error_types'].items():
            lines.append(f'scylladb_store_errors_by_type_total{{error_type="{error_type}"}} {count}')
        lines.append("")

        # Prepared statements count
        lines.append("# HELP scylladb_store_prepared_statements Number of prepared statements")
        lines.append("# TYPE scylladb_store_prepared_statements gauge")
        lines.append(f"scylladb_store_prepared_statements {len(self._prepared_statements)}")
        lines.append("")

        return "\n".join(lines)

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
        return asyncio.run(
            self.aget(namespace, key, refresh_ttl=refresh_ttl)
        )

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
        return asyncio.run(
            self.aput(namespace, key, value, index, ttl=ttl)
        )

    def delete(
        self,
        namespace: tuple[str, ...],
        key: str,
    ) -> None:
        """Synchronous wrapper for adelete()."""
        return asyncio.run(
            self.adelete(namespace, key)
        )

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
        return asyncio.run(
            self.asearch(
                namespace_prefix,
                query=query,
                filter=filter,
                limit=limit,
                offset=offset,
                refresh_ttl=refresh_ttl,
            )
        )

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
        return asyncio.run(
            self.alist_namespaces(
                prefix=prefix,
                suffix=suffix,
                max_depth=max_depth,
                limit=limit,
                offset=offset,
            )
        )

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """Synchronous wrapper for abatch()."""
        return asyncio.run(
            self.abatch(ops)
        )

    # Internal helper methods

    async def _execute_prepared(self, statement_name: str, parameters: tuple) -> list:
        """
        Execute a pre-prepared statement asynchronously with error handling and metrics.

        Args:
            statement_name: Name of the prepared statement
            parameters: Query parameters

        Returns:
            List of result rows

        Raises:
            StoreConnectionError: If no hosts are available
            StoreTimeoutError: If operation times out
            StoreUnavailableError: If required replicas are unavailable
            StoreQueryError: If query execution fails
            StoreAuthenticationError: If authentication/authorization fails
        """
        prepared = self._prepared_statements.get(statement_name)
        if not prepared:
            raise StoreValidationError(
                f"Statement '{statement_name}' not prepared. Call setup() first.",
                field="statement_name",
                value=statement_name
            )

        # Start timing
        start_time = time.perf_counter()

        loop = asyncio.get_running_loop()
        asyncio_future = loop.create_future()

        response_future = self.session.execute_async(prepared, parameters)

        def on_success(result):
            loop.call_soon_threadsafe(asyncio_future.set_result, result)

        def on_error(error):
            loop.call_soon_threadsafe(asyncio_future.set_exception, error)

        response_future.add_callbacks(on_success, on_error)

        try:
            result = await asyncio_future

            # Record successful query
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=True)

            return list(result) if result else []

        except NoHostAvailable as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type="NoHostAvailable")
            raise StoreConnectionError(
                "No hosts available for query execution",
                original_error=e
            )

        except (ReadTimeout, WriteTimeout) as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            operation = "read" if isinstance(e, ReadTimeout) else "write"
            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type=f"{operation}Timeout")
            raise StoreTimeoutError(
                f"{operation.capitalize()} operation timed out",
                original_error=e,
                operation_type=operation
            )

        except OperationTimedOut as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type="OperationTimedOut")
            raise StoreTimeoutError(
                "Client-side operation timeout",
                original_error=e
            )

        except Unavailable as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type="Unavailable")

            # Extract details from exception if available
            consistency = getattr(e, 'consistency', None)
            required = getattr(e, 'required_replicas', None)
            alive = getattr(e, 'alive_replicas', None)

            raise StoreUnavailableError(
                "Required replicas unavailable",
                original_error=e,
                consistency_level=str(consistency) if consistency else None,
                required_replicas=required,
                alive_replicas=alive
            )

        except (ReadFailure, WriteFailure, CoordinationFailure) as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            operation = "unknown"
            if isinstance(e, ReadFailure):
                operation = "read"
            elif isinstance(e, WriteFailure):
                operation = "write"

            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type=f"{operation}Failure")

            raise StoreQueryError(
                f"Coordination failure during {operation} operation",
                original_error=e
            )

        except (Unauthorized, AuthenticationFailed) as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type="AuthError")
            raise StoreAuthenticationError(
                "Authentication or authorization failed",
                original_error=e
            )

        except (InvalidRequest, ConfigurationException) as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type="ValidationError")
            raise StoreValidationError(
                f"Invalid query or configuration: {e}",
                original_error=e
            )

        except AlreadyExists as e:
            # This is actually not an error for our use case (using IF NOT EXISTS)
            # But handle it gracefully
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=True)
            logger.debug(f"AlreadyExists exception (expected): {e}")
            return []

        except RequestExecutionException as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type="RequestExecution")
            raise StoreQueryError(
                f"Query execution failed: {e}",
                original_error=e
            )

        except DriverException as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type="DriverError")
            raise ScyllaDBStoreError(
                f"Driver error: {e}",
                original_error=e
            )

        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            await self.metrics.record_query(statement_name, latency_ms, success=False, error_type="UnexpectedError")
            logger.error(f"Unexpected error in _execute_prepared: {e}", exc_info=e)
            raise ScyllaDBStoreError(
                f"Unexpected error: {e}",
                original_error=e
            )

    async def _prepare_statements(self) -> None:
        """
        Prepare all commonly used statements once during setup.

        This follows the best practice of preparing statements once and reusing them,
        rather than preparing on-demand which wastes resources.
        """
        loop = asyncio.get_running_loop()

        statements = {
            # Single item operations
            "get": "SELECT * FROM store WHERE prefix = ? AND key = ?",
            "put_no_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
            "put_with_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                USING TTL ?
            """,
            "delete": "DELETE FROM store WHERE prefix = ? AND key = ?",
            "refresh_ttl": """
                UPDATE store USING TTL ?
                SET updated_at = ?
                WHERE prefix = ? AND key = ?
            """,

            # Batch operations
            "batch_get": "SELECT * FROM store WHERE prefix = ? AND key = ?",
            "batch_put_no_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
            "batch_put_with_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                USING TTL ?
            """,

            # Lightweight Transaction (LWT) operations
            "put_if_not_exists_no_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                IF NOT EXISTS
            """,
            "put_if_not_exists_with_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                IF NOT EXISTS
                USING TTL ?
            """,
            "update_if_exists_no_ttl": """
                UPDATE store
                SET value = ?, updated_at = ?
                WHERE prefix = ? AND key = ?
                IF EXISTS
            """,
            "update_if_exists_with_ttl": """
                UPDATE store USING TTL ?
                SET value = ?, updated_at = ?
                WHERE prefix = ? AND key = ?
                IF EXISTS
            """,
            "cas_update_no_ttl": """
                UPDATE store
                SET value = ?, updated_at = ?
                WHERE prefix = ? AND key = ?
                IF value = ?
            """,
            "cas_update_with_ttl": """
                UPDATE store USING TTL ?
                SET value = ?, updated_at = ?
                WHERE prefix = ? AND key = ?
                IF value = ?
            """,
            "delete_if_exists": """
                DELETE FROM store
                WHERE prefix = ? AND key = ?
                IF EXISTS
            """,
            "delete_if_value": """
                DELETE FROM store
                WHERE prefix = ? AND key = ?
                IF value = ?
            """,
        }

        # Prepare all statements concurrently
        for name, query in statements.items():
            prepared = await loop.run_in_executor(None, self.session.prepare, query)
            self._prepared_statements[name] = prepared

        logger.info(f"Prepared {len(statements)} statements for reuse")

    async def _execute_async(self, query: str | SimpleStatement, parameters: tuple | None = None) -> list:
        """
        Execute a CQL query asynchronously using AsyncioConnection.

        With AsyncioConnection, actual I/O happens non-blocking in the driver's
        internal asyncio event loop (separate thread). We bridge ResponseFuture
        to our application event loop using asyncio.Future.

        Args:
            query: CQL query string or SimpleStatement object (with fetch_size, etc.)
            parameters: Query parameters (only used with string queries)
        """
        # Handle SimpleStatement objects directly (they may have fetch_size set)
        if isinstance(query, SimpleStatement):
            response_future = self.session.execute_async(query)
        else:
            # Use prepared statements for better performance (except for DDL statements)
            is_ddl = any(keyword in query.upper() for keyword in ['CREATE', 'DROP', 'ALTER', 'TRUNCATE'])

            prepared = None
            if not is_ddl and parameters:
                # Convert %s placeholders to ? for prepared statements
                prepared_query = query.replace('%s', '?')

                if prepared_query not in self._prepared_statements:
                    # Prepare statement - still synchronous API
                    loop = asyncio.get_running_loop()
                    prepared = await loop.run_in_executor(
                        None, self.session.prepare, prepared_query
                    )
                    self._prepared_statements[prepared_query] = prepared
                else:
                    prepared = self._prepared_statements[prepared_query]

            # Execute async and convert ResponseFuture to awaitable
            if prepared and parameters:
                response_future = self.session.execute_async(prepared, parameters)
            elif parameters:
                response_future = self.session.execute_async(query, parameters)
            else:
                response_future = self.session.execute_async(query)

        # Bridge driver's ResponseFuture to asyncio.Future
        # This is non-blocking: the actual I/O happens in AsyncioConnection's event loop
        loop = asyncio.get_running_loop()
        asyncio_future = loop.create_future()

        def on_success(result):
            loop.call_soon_threadsafe(asyncio_future.set_result, result)

        def on_error(error):
            loop.call_soon_threadsafe(asyncio_future.set_exception, error)

        response_future.add_callbacks(on_success, on_error)

        result = await asyncio_future
        return list(result) if result else []

    def _namespace_to_prefix(self, namespace: tuple[str, ...]) -> str:
        """Convert namespace tuple to dot-separated prefix string."""
        return ".".join(namespace)

    def _prefix_to_namespace(self, prefix: str) -> tuple[str, ...]:
        """Convert dot-separated prefix string to namespace tuple."""
        return tuple(prefix.split("."))

    def _validate_namespace(self, namespace: tuple[str, ...]) -> None:
        """
        Validate namespace tuple with comprehensive checks.

        Args:
            namespace: Tuple of namespace labels

        Raises:
            StoreValidationError: If namespace is invalid
        """
        if not namespace:
            raise StoreValidationError(
                "Namespace cannot be empty",
                field="namespace",
                value=namespace
            )

        if not isinstance(namespace, tuple):
            raise StoreValidationError(
                f"Namespace must be a tuple, got {type(namespace).__name__}",
                field="namespace",
                value=namespace
            )

        if not all(isinstance(label, str) for label in namespace):
            invalid_types = [type(label).__name__ for label in namespace if not isinstance(label, str)]
            raise StoreValidationError(
                f"All namespace labels must be strings, found types: {invalid_types}",
                field="namespace",
                value=namespace
            )

        # Check depth limit
        if len(namespace) > ValidationLimits.MAX_NAMESPACE_DEPTH:
            raise StoreValidationError(
                f"Namespace depth ({len(namespace)}) exceeds maximum ({ValidationLimits.MAX_NAMESPACE_DEPTH})",
                field="namespace",
                value=namespace
            )

        # Check each label
        for i, label in enumerate(namespace):
            if not label:
                raise StoreValidationError(
                    f"Namespace label at position {i} cannot be empty",
                    field=f"namespace[{i}]",
                    value=label
                )

            if "." in label:
                raise StoreValidationError(
                    f"Namespace label at position {i} cannot contain periods ('.')",
                    field=f"namespace[{i}]",
                    value=label
                )

            if len(label) > ValidationLimits.MAX_NAMESPACE_LABEL_LENGTH:
                raise StoreValidationError(
                    f"Namespace label at position {i} exceeds maximum length ({ValidationLimits.MAX_NAMESPACE_LABEL_LENGTH})",
                    field=f"namespace[{i}]",
                    value=label
                )

        # Reserved namespace check
        if namespace[0] == "langgraph":
            raise StoreValidationError(
                "Root namespace label cannot be 'langgraph' (reserved)",
                field="namespace[0]",
                value=namespace[0]
            )

    def _validate_key(self, key: str) -> None:
        """
        Validate key string.

        Args:
            key: Item key

        Raises:
            StoreValidationError: If key is invalid
        """
        if not isinstance(key, str):
            raise StoreValidationError(
                f"Key must be a string, got {type(key).__name__}",
                field="key",
                value=key
            )

        if not key:
            raise StoreValidationError(
                "Key cannot be empty",
                field="key",
                value=key
            )

        if len(key) > ValidationLimits.MAX_KEY_LENGTH:
            raise StoreValidationError(
                f"Key length ({len(key)}) exceeds maximum ({ValidationLimits.MAX_KEY_LENGTH})",
                field="key",
                value=key
            )

    def _validate_value(self, value: dict[str, Any]) -> None:
        """
        Validate value dictionary and size.

        Args:
            value: Dictionary to store

        Raises:
            StoreValidationError: If value is invalid
        """
        if not isinstance(value, dict):
            raise StoreValidationError(
                f"Value must be a dictionary, got {type(value).__name__}",
                field="value",
                value=type(value).__name__
            )

        # Serialize to check size
        try:
            value_json = json.dumps(value)
        except (TypeError, ValueError) as e:
            raise StoreValidationError(
                f"Value is not JSON-serializable: {e}",
                field="value",
                value=value,
                original_error=e
            )

        value_size = len(value_json.encode('utf-8'))

        if value_size > ValidationLimits.MAX_VALUE_SIZE_BYTES:
            raise StoreValidationError(
                f"Value size ({value_size} bytes) exceeds maximum ({ValidationLimits.MAX_VALUE_SIZE_BYTES} bytes)",
                field="value",
                value=f"{value_size} bytes"
            )

        # Warn if approaching limit
        if value_size > ValidationLimits.WARN_VALUE_SIZE_BYTES:
            logger.warning(
                f"Large value detected ({value_size} bytes). "
                f"Consider breaking into smaller items for better performance."
            )

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
        now = datetime.now(timezone.utc)
        ttl_seconds = ttl_minutes * 60
        await self._execute_prepared("refresh_ttl", (ttl_seconds, now, prefix, key))

    # Batch operation helpers

    async def _batch_get_ops(
        self,
        get_ops: Sequence[tuple[int, GetOp]],
        results: list[Result],
    ) -> None:
        """Execute batch GET operations using execute_concurrent_with_args."""
        if not get_ops:
            return

        # Use pre-prepared statement
        prepared = self._prepared_statements.get("batch_get")
        if not prepared:
            raise RuntimeError("Statements not prepared. Call setup() first.")

        # Build parameters for concurrent execution
        params_list = []
        index_map = []
        for idx, op in get_ops:
            prefix = self._namespace_to_prefix(op.namespace)
            params_list.append((prefix, op.key))
            index_map.append((idx, op))

        # Execute concurrently using driver's built-in concurrent execution
        loop = asyncio.get_running_loop()
        execute_results = await loop.run_in_executor(
            None,
            lambda: list(execute_concurrent_with_args(
                self.session, prepared, params_list, concurrency=50
            ))
        )

        # Process results
        for (idx, op), (success, result) in zip(index_map, execute_results):
            if success and result:
                row = result[0]
                should_refresh = op.refresh_ttl if op.refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

                # Refresh TTL if needed
                if should_refresh and row.ttl_minutes:
                    prefix = self._namespace_to_prefix(op.namespace)
                    await self._refresh_ttl(prefix, op.key, row.ttl_minutes)

                results[idx] = self._row_to_item(row, op.namespace, op.key)
            else:
                results[idx] = None

    async def _batch_put_atomic(
        self,
        put_ops: Sequence[PutOp],
        results: list[Result],
        batch_type: str = "UNLOGGED",
    ) -> None:
        """
        Execute batch PUT operations atomically using BatchStatement.

        Args:
            put_ops: Sequence of PutOp operations
            results: List to store results
            batch_type: Type of batch - "LOGGED" (atomic, slower) or "UNLOGGED" (faster, default)

        Note:
            - LOGGED: Full atomicity across partitions, performance cost
            - UNLOGGED: Better performance, atomic within partition only
        """
        if not put_ops:
            return

        from cassandra.query import BatchStatement, BatchType

        # Select batch type based on parameter
        if batch_type == "LOGGED":
            bt = BatchType.LOGGED
        elif batch_type == "UNLOGGED":
            bt = BatchType.UNLOGGED
        else:
            raise ValueError(f"Invalid batch_type: {batch_type}. Use 'LOGGED' or 'UNLOGGED'")

        batch = BatchStatement(batch_type=bt)

        now = datetime.now(timezone.utc)

        for op in put_ops:
            prefix = self._namespace_to_prefix(op.namespace)
            value_json = json.dumps(op.value)

            # Determine TTL
            ttl_minutes = None
            ttl_seconds = None

            if op.ttl is not NOT_PROVIDED and op.ttl is not None:
                ttl_seconds = int(op.ttl)
                ttl_minutes = int(op.ttl / 60)
            elif self.ttl_config.get("default_ttl"):
                default_ttl_seconds = self.ttl_config["default_ttl"]
                ttl_seconds = int(default_ttl_seconds)
                ttl_minutes = int(default_ttl_seconds / 60)

            # Add to batch with appropriate prepared statement
            if ttl_seconds:
                prepared = self._prepared_statements.get("batch_put_with_ttl")
                batch.add(prepared, (prefix, op.key, value_json, now, now, ttl_minutes, ttl_seconds))
            else:
                prepared = self._prepared_statements.get("batch_put_no_ttl")
                batch.add(prepared, (prefix, op.key, value_json, now, now, ttl_minutes))

        # Execute batch atomically
        loop = asyncio.get_running_loop()
        asyncio_future = loop.create_future()

        response_future = self.session.execute_async(batch)

        def on_success(result):
            loop.call_soon_threadsafe(asyncio_future.set_result, result)

        def on_error(error):
            loop.call_soon_threadsafe(asyncio_future.set_exception, error)

        response_future.add_callbacks(on_success, on_error)

        try:
            await asyncio_future
            # Set all results to None (batch writes don't return individual results)
            for idx in range(len(put_ops)):
                results[idx] = None
            logger.info(f"Atomic batch executed: {len(put_ops)} PUT operations ({batch_type})")
        except Exception as e:
            logger.error(f"Atomic batch failed ({batch_type}): {e}")
            raise StoreQueryError(f"Atomic batch execution failed ({batch_type})", original_error=e)

    async def _batch_put_ops(
        self,
        put_ops: Sequence[tuple[int, PutOp]],
        results: list[Result],
    ) -> None:
        """Execute batch PUT operations using execute_concurrent_with_args (fallback for mixed batches)."""
        if not put_ops:
            return

        # Group by whether TTL is set (different queries)
        ops_with_ttl = []
        ops_without_ttl = []

        for idx, op in put_ops:
            ttl_minutes = None
            if op.ttl is not NOT_PROVIDED and op.ttl is not None:
                ttl_minutes = int(op.ttl)
            elif self.ttl_config.get("default_ttl"):
                ttl_minutes = int(self.ttl_config["default_ttl"])

            if ttl_minutes:
                ops_with_ttl.append((idx, op, ttl_minutes))
            else:
                ops_without_ttl.append((idx, op))

        # Execute operations without TTL
        if ops_without_ttl:
            prepared = self._prepared_statements.get("batch_put_no_ttl")
            if not prepared:
                raise RuntimeError("Statements not prepared. Call setup() first.")

            params_list = []
            now = datetime.now(timezone.utc)
            for idx, op in ops_without_ttl:
                prefix = self._namespace_to_prefix(op.namespace)
                value_json = json.dumps(op.value)
                params_list.append((prefix, op.key, value_json, now, now, None))

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: list(execute_concurrent_with_args(
                    self.session, prepared, params_list, concurrency=50
                ))
            )

            for idx, _ in ops_without_ttl:
                results[idx] = None

        # Execute operations with TTL
        if ops_with_ttl:
            prepared = self._prepared_statements.get("batch_put_with_ttl")
            if not prepared:
                raise RuntimeError("Statements not prepared. Call setup() first.")

            params_list = []
            now = datetime.now(timezone.utc)
            for idx, op, ttl_minutes in ops_with_ttl:
                prefix = self._namespace_to_prefix(op.namespace)
                value_json = json.dumps(op.value)
                ttl_seconds = ttl_minutes * 60
                params_list.append((prefix, op.key, value_json, now, now, ttl_minutes, ttl_seconds))

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: list(execute_concurrent_with_args(
                    self.session, prepared, params_list, concurrency=50
                ))
            )

            for idx, _, _ in ops_with_ttl:
                results[idx] = None

    async def _batch_search_ops(
        self,
        search_ops: Sequence[tuple[int, SearchOp]],
        results: list[Result],
    ) -> None:
        """Execute batch SEARCH operations concurrently."""
        tasks = []
        for idx, op in search_ops:
            task = self.asearch(
                op.namespace_prefix,
                query=op.query,
                filter=op.filter,
                limit=op.limit,
                offset=op.offset,
                refresh_ttl=op.refresh_ttl,
            )
            tasks.append((idx, task))

        # Execute all SEARCH operations concurrently
        completed = await asyncio.gather(*[task for _, task in tasks])

        for (idx, _), result in zip(tasks, completed):
            results[idx] = result

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
