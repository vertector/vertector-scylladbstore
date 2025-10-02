"""
ScyllaDB implementation of LangGraph's BaseStore interface with hybrid semantic search.

This module extends the base ScyllaDB store with semantic search capabilities using
Google's Gemini embeddings via LangChain.
"""

import asyncio
import json
import logging
import pickle
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Iterable,
    Literal,
    Sequence,
    TypedDict,
)

from cassandra.cluster import Cluster, Session

# Import base store
from scylladb_store import (
    AsyncScyllaDBStore,
    Item,
    SearchItem,
    Op,
    GetOp,
    PutOp,
    SearchOp,
    ListNamespacesOp,
    Result,
    PoolConfig,
    TTLConfig,
    NOT_PROVIDED,
)

try:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    import numpy as np
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    GoogleGenerativeAIEmbeddings = None
    np = None

try:
    import hnswlib
    HNSWLIB_AVAILABLE = True
except ImportError:
    HNSWLIB_AVAILABLE = False
    hnswlib = None


logger = logging.getLogger(__name__)


class EmbeddingConfig(TypedDict, total=False):
    """Configuration for embedding generation and vector search."""
    model: str  # Gemini model name (default: "models/gemini-embedding-001")
    dims: int  # Embedding dimensions (auto-detected if not provided)
    distance: Literal["cosine", "euclidean", "dot_product"]  # Similarity metric
    fields: list[str] | None  # Fields to embed (default: ["$"] = entire value)
    api_key: str | None  # Google API key (or set GOOGLE_API_KEY env var)


class HybridAsyncScyllaDBStore(AsyncScyllaDBStore):
    """
    Extended ScyllaDB store with hybrid search capabilities.

    Combines vanilla ScyllaDB filter-based search with semantic search
    using Google Gemini embeddings with task-specific optimization.

    Features:
        - Vanilla filter-based search (exact matches, comparisons)
        - Semantic search with Gemini embeddings
        - Task-specific embeddings (RETRIEVAL_QUERY for queries, RETRIEVAL_DOCUMENT for documents)
        - Hybrid search combining both approaches
        - In-memory HNSW index for fast similarity search
        - Automatic embedding generation

    Example:
        embedding_config = EmbeddingConfig(
            model="models/gemini-embedding-001",
            distance="cosine",
            fields=["text", "content"],
            api_key="your-google-api-key"  # or set GOOGLE_API_KEY env var
        )

        async with HybridAsyncScyllaDBStore.from_contact_points(
            contact_points=["127.0.0.1"],
            keyspace="semantic_store",
            embedding_config=embedding_config
        ) as store:
            await store.setup()

            # Semantic search
            results = await store.asearch(
                ("docs",),
                query="machine learning algorithms",
                limit=5
            )
    """

    def __init__(
        self,
        session: Session,
        keyspace: str,
        *,
        deserializer: Callable[[str], dict[str, Any]] | None = None,
        ttl: TTLConfig | None = None,
        embedding_config: EmbeddingConfig | None = None,
    ) -> None:
        """
        Initialize hybrid ScyllaDB store with semantic search.

        Args:
            session: ScyllaDB/Cassandra session
            keyspace: Keyspace name
            deserializer: Optional JSON deserializer
            ttl: Optional TTL configuration
            embedding_config: Optional embedding configuration for semantic search
        """
        super().__init__(session, keyspace, deserializer=deserializer, ttl=ttl)

        self.embedding_config = embedding_config or {}
        self.query_embedding_model = None  # For query embeddings (RETRIEVAL_QUERY)
        self.doc_embedding_model = None    # For document embeddings (RETRIEVAL_DOCUMENT)
        self.vector_index = None
        self.id_to_key = {}  # Maps vector index IDs to (namespace, key) tuples
        self.key_to_id = {}  # Maps (namespace, key) to vector index IDs
        self.next_id = 0

        # Initialize embedding models if config provided
        if self.embedding_config:
            self._init_embedding_models()
            self._init_vector_index()

    def _init_embedding_models(self):
        """Initialize separate embedding models for queries and documents."""
        if not EMBEDDINGS_AVAILABLE:
            raise ImportError(
                "langchain-google-genai is required for semantic search. "
                "Install with: pip install langchain-google-genai"
            )

        model_name = self.embedding_config.get("model", "models/gemini-embedding-001")
        api_key = self.embedding_config.get("api_key")

        logger.info(f"Loading embedding models: {model_name}")

        # Query embeddings (for search queries)
        self.query_embedding_model = GoogleGenerativeAIEmbeddings(
            model=model_name,
            task_type="RETRIEVAL_QUERY",
            google_api_key=api_key
        )

        # Document embeddings (for stored documents)
        self.doc_embedding_model = GoogleGenerativeAIEmbeddings(
            model=model_name,
            task_type="RETRIEVAL_DOCUMENT",
            google_api_key=api_key
        )

        # Auto-detect dimensions if not provided
        if "dims" not in self.embedding_config:
            test_embedding = self.query_embedding_model.embed_query("test")
            self.embedding_config["dims"] = len(test_embedding)
            logger.info(f"Auto-detected embedding dimensions: {self.embedding_config['dims']}")

    def _init_vector_index(self):
        """Initialize the HNSW vector index."""
        if not HNSWLIB_AVAILABLE:
            raise ImportError(
                "hnswlib is required for vector search. "
                "Install with: pip install hnswlib"
            )

        dims = self.embedding_config.get("dims", 768)
        distance = self.embedding_config.get("distance", "cosine")

        # Map distance metric to hnswlib space
        space_map = {
            "cosine": "cosine",
            "euclidean": "l2",
            "dot_product": "ip"
        }
        space = space_map.get(distance, "cosine")

        logger.info(f"Initializing HNSW index: dims={dims}, space={space}")
        self.vector_index = hnswlib.Index(space=space, dim=dims)
        self.vector_index.init_index(max_elements=100000, ef_construction=200, M=16)
        self.vector_index.set_ef(50)  # Search parameter

    @classmethod
    @asynccontextmanager
    async def from_contact_points(
        cls,
        contact_points: list[str],
        keyspace: str,
        *,
        pool_config: PoolConfig | None = None,
        ttl: TTLConfig | None = None,
        embedding_config: EmbeddingConfig | None = None,
    ) -> AsyncIterator["HybridAsyncScyllaDBStore"]:
        """
        Create HybridAsyncScyllaDBStore from contact points.

        Args:
            contact_points: List of ScyllaDB node addresses
            keyspace: Keyspace name
            pool_config: Optional connection pool configuration
            ttl: Optional TTL configuration
            embedding_config: Optional embedding configuration

        Yields:
            HybridAsyncScyllaDBStore instance
        """
        pool_config = pool_config or {}

        cluster = Cluster(contact_points=contact_points, **pool_config)

        store = None
        try:
            session = await asyncio.get_event_loop().run_in_executor(
                None, cluster.connect
            )

            store = cls(
                session=session,
                keyspace=keyspace,
                ttl=ttl,
                embedding_config=embedding_config,
            )

            yield store

        finally:
            if store is not None and hasattr(store, '_ttl_sweeper_task') and store._ttl_sweeper_task:
                await store.stop_ttl_sweeper()

            await asyncio.get_event_loop().run_in_executor(
                None, cluster.shutdown
            )

    async def setup(self) -> None:
        """
        Set up keyspace and tables, including embedding storage.

        Extends base setup to add embedding column.
        """
        await super().setup()

        # Add embedding column if semantic search is enabled
        if self.embedding_config:
            try:
                await self._execute_async("""
                    ALTER TABLE store ADD embedding blob
                """)
                logger.info("Added embedding column to store table")
            except Exception as e:
                # Column might already exist
                if "already exists" not in str(e).lower():
                    logger.warning(f"Could not add embedding column: {e}")

    def _generate_embedding(self, value: dict[str, Any]) -> list[float]:
        """
        Generate document embedding for a value.

        Args:
            value: Dictionary to generate embedding for

        Returns:
            Embedding vector as list of floats
        """
        if not self.doc_embedding_model:
            raise RuntimeError("Document embedding model not initialized")

        # Extract fields to embed
        fields = self.embedding_config.get("fields", ["$"])

        if fields == ["$"]:
            # Embed entire value
            text = json.dumps(value, sort_keys=True)
        else:
            # Embed specific fields
            texts = []
            for field in fields:
                field_value = self._get_nested_value(value, field)
                if field_value is not None:
                    texts.append(str(field_value))
            text = " ".join(texts)

        # Generate document embedding using RETRIEVAL_DOCUMENT task type
        embeddings = self.doc_embedding_model.embed_documents([text])
        return embeddings[0]

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
        Store or update an item with optional embedding generation.

        Args:
            namespace: Hierarchical path
            key: Unique identifier
            value: Dictionary to store
            index: Controls embedding generation:
                - None: Use config default
                - False: Skip embedding generation
                - list[str]: Embed specific fields
            ttl: Time-to-live in minutes
        """
        self._validate_namespace(namespace)

        prefix = self._namespace_to_prefix(namespace)
        value_json = json.dumps(value)
        now = datetime.now(timezone.utc)

        # Generate embedding if enabled
        embedding_blob = None
        if self.embedding_config and index != False:
            # Override fields if specified
            original_fields = self.embedding_config.get("fields")
            if isinstance(index, list):
                self.embedding_config["fields"] = index

            embedding = await asyncio.get_event_loop().run_in_executor(
                None, self._generate_embedding, value
            )
            embedding_blob = pickle.dumps(embedding)

            # Restore original fields
            if isinstance(index, list):
                self.embedding_config["fields"] = original_fields

            # Add to vector index
            item_id = self._get_or_create_id(namespace, key)
            self.vector_index.add_items(
                np.array([embedding], dtype=np.float32),
                np.array([item_id])
            )

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

        # Build INSERT query with embedding
        if ttl_seconds:
            query = """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                USING TTL %s
            """
            params = (prefix, key, value_json, now, now, ttl_minutes, embedding_blob, ttl_seconds)
        else:
            query = """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_minutes, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            params = (prefix, key, value_json, now, now, ttl_minutes, embedding_blob)

        await self._execute_async(query, params)

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
        Hybrid search combining semantic and filter-based search.

        Args:
            namespace_prefix: Path prefix to search within
            query: Natural language query for semantic search
            filter: Key-value pairs for filtering results
            limit: Maximum items to return
            offset: Number of items to skip
            refresh_ttl: Whether to refresh TTL on read

        Returns:
            List of SearchItem objects with similarity scores

        Behavior:
            - If query provided and embeddings enabled: Semantic search
            - If only filter provided: Filter-based search
            - If both: Hybrid (semantic + filter)
        """
        self._validate_namespace(namespace_prefix)

        # Semantic search path
        if query and self.embedding_config and self.query_embedding_model:
            return await self._semantic_search(
                namespace_prefix,
                query=query,
                filter=filter,
                limit=limit,
                offset=offset,
                refresh_ttl=refresh_ttl
            )

        # Fallback to vanilla filter-based search
        return await super().asearch(
            namespace_prefix,
            query=None,
            filter=filter,
            limit=limit,
            offset=offset,
            refresh_ttl=refresh_ttl
        )

    async def _semantic_search(
        self,
        namespace_prefix: tuple[str, ...],
        *,
        query: str,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
        refresh_ttl: bool | None = None,
    ) -> list[SearchItem]:
        """
        Perform semantic search using vector similarity.

        Args:
            namespace_prefix: Path prefix to search within
            query: Natural language query
            filter: Optional filters to apply
            limit: Maximum items to return
            offset: Pagination offset
            refresh_ttl: Whether to refresh TTL

        Returns:
            List of SearchItem objects sorted by similarity score
        """
        # Generate query embedding using RETRIEVAL_QUERY task type
        query_embedding = await asyncio.get_event_loop().run_in_executor(
            None, self.query_embedding_model.embed_query, query
        )

        # Search vector index (get more than needed for filtering)
        k = min(limit * 10, self.vector_index.get_current_count())
        if k == 0:
            return []

        labels, distances = self.vector_index.knn_query(
            np.array([query_embedding], dtype=np.float32),
            k=k
        )

        # Convert distances to similarity scores (1.0 = perfect match, 0.0 = no match)
        distance_metric = self.embedding_config.get("distance", "cosine")
        if distance_metric == "cosine":
            scores = 1.0 - distances[0]  # Cosine distance to similarity
        elif distance_metric == "euclidean":
            scores = 1.0 / (1.0 + distances[0])  # Inverse distance
        elif distance_metric == "dot_product":
            scores = (distances[0] + 1.0) / 2.0  # Normalize to [0, 1]
        else:
            scores = 1.0 - distances[0]

        # Fetch items from ScyllaDB
        items = []
        prefix = self._namespace_to_prefix(namespace_prefix)
        should_refresh = refresh_ttl if refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

        for idx, (item_id, score) in enumerate(zip(labels[0], scores)):
            # Get namespace and key from ID
            if item_id not in self.id_to_key:
                continue

            item_namespace, item_key = self.id_to_key[item_id]

            # Check namespace prefix match
            item_prefix = self._namespace_to_prefix(item_namespace)
            if not item_prefix.startswith(prefix):
                continue

            # Fetch from ScyllaDB
            item = await self.aget(item_namespace, item_key, refresh_ttl=should_refresh)
            if item is None:
                continue

            # Apply filters
            if filter and not self._matches_filter(item.value, filter):
                continue

            # Create SearchItem with score
            search_item = SearchItem(
                value=item.value,
                key=item.key,
                namespace=item.namespace,
                created_at=item.created_at,
                updated_at=item.updated_at,
                score=float(score)
            )
            items.append(search_item)

            # Stop if we have enough items
            if len(items) >= limit + offset:
                break

        # Apply offset and limit
        return items[offset:offset + limit]

    def _get_or_create_id(self, namespace: tuple[str, ...], key: str) -> int:
        """
        Get or create a unique ID for a (namespace, key) pair.

        Args:
            namespace: Namespace tuple
            key: Item key

        Returns:
            Unique integer ID
        """
        lookup_key = (namespace, key)
        if lookup_key not in self.key_to_id:
            item_id = self.next_id
            self.key_to_id[lookup_key] = item_id
            self.id_to_key[item_id] = lookup_key
            self.next_id += 1
            return item_id
        return self.key_to_id[lookup_key]

    async def rebuild_index(self) -> int:
        """
        Rebuild the vector index from all items in the database.

        Returns:
            Number of items indexed

        Use this to initialize the index when starting with existing data
        or to recover from index corruption.
        """
        if not self.embedding_config:
            raise RuntimeError("Embedding configuration not set")

        logger.info("Rebuilding vector index from database...")

        # Clear existing index
        self._init_vector_index()
        self.id_to_key.clear()
        self.key_to_id.clear()
        self.next_id = 0

        # Fetch all items
        query = "SELECT prefix, key, value, embedding FROM store"
        results = await self._execute_async(query)

        count = 0
        for row in results:
            prefix = row.prefix
            key = row.key
            value = json.loads(row.value)
            namespace = self._prefix_to_namespace(prefix)

            # Generate or use existing embedding
            if row.embedding:
                embedding = pickle.loads(row.embedding)
            else:
                embedding = self._generate_embedding(value)
                # Update database with embedding
                await self._execute_async(
                    "UPDATE store SET embedding = %s WHERE prefix = %s AND key = %s",
                    (pickle.dumps(embedding), prefix, key)
                )

            # Add to index
            item_id = self._get_or_create_id(namespace, key)
            self.vector_index.add_items(
                np.array([embedding], dtype=np.float32),
                np.array([item_id])
            )
            count += 1

        logger.info(f"Rebuilt index with {count} items")
        return count

    def save_index(self, path: str) -> None:
        """
        Save the vector index and ID mappings to disk.

        Args:
            path: Directory path to save index
        """
        if not self.vector_index:
            raise RuntimeError("Vector index not initialized")

        path_obj = Path(path)
        path_obj.mkdir(parents=True, exist_ok=True)

        # Save HNSW index
        self.vector_index.save_index(str(path_obj / "vector.index"))

        # Save ID mappings
        with open(path_obj / "id_mappings.pkl", "wb") as f:
            pickle.dump({
                "id_to_key": self.id_to_key,
                "key_to_id": self.key_to_id,
                "next_id": self.next_id
            }, f)

        logger.info(f"Saved index to {path}")

    def load_index(self, path: str) -> None:
        """
        Load the vector index and ID mappings from disk.

        Args:
            path: Directory path to load index from
        """
        if not self.vector_index:
            self._init_vector_index()

        path_obj = Path(path)

        # Load HNSW index
        self.vector_index.load_index(str(path_obj / "vector.index"))

        # Load ID mappings
        with open(path_obj / "id_mappings.pkl", "rb") as f:
            mappings = pickle.load(f)
            self.id_to_key = mappings["id_to_key"]
            self.key_to_id = mappings["key_to_id"]
            self.next_id = mappings["next_id"]

        logger.info(f"Loaded index from {path} ({len(self.id_to_key)} items)")
