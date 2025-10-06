"""
ScyllaDB implementation of LangGraph's BaseStore interface.

This module provides AsyncScyllaDBStore, which implements the same interface
as LangGraph's AsyncPostgresStore but uses ScyllaDB as the backend.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict, OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Literal,
    Sequence,
    TypedDict,
    Union,
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

# Import observability and rate limiting modules
from vertector_scylladbstore.observability import Tracer, EnhancedMetrics, AlertManager, AlertSeverity
from vertector_scylladbstore.rate_limiter import TokenBucketRateLimiter, RateLimitExceeded

# Qdrant integration (optional)
try:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    AsyncQdrantClient = None

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


# LRU Cache implementation for embeddings
class LRUCache:
    """
    Least Recently Used (LRU) cache with statistics tracking.

    Significantly better than FIFO for embedding cache as it keeps frequently
    accessed embeddings in memory, improving cache hit rate by 20-50%.
    """

    def __init__(self, max_size: int):
        self.cache: OrderedDict = OrderedDict()
        self.max_size = max_size
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> list[float] | None:
        """Get value from cache, marking it as recently used."""
        if key in self.cache:
            self.hits += 1
            # Move to end (most recently used)
            self.cache.move_to_end(key)
            return self.cache[key]
        self.misses += 1
        return None

    def put(self, key: str, value: list[float]) -> None:
        """Put value in cache, evicting LRU item if at capacity."""
        if key in self.cache:
            # Update existing - move to end
            self.cache.move_to_end(key)
            self.cache[key] = value
        else:
            # New entry
            if len(self.cache) >= self.max_size:
                # Remove least recently used (first item)
                self.cache.popitem(last=False)
            self.cache[key] = value

    def __contains__(self, key: str) -> bool:
        """Check if key exists in cache."""
        return key in self.cache

    def __len__(self) -> int:
        """Get current cache size."""
        return len(self.cache)

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics for monitoring."""
        total = self.hits + self.misses
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hits / total if total > 0 else 0.0,
        }


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


class StoreConfigurationError(ScyllaDBStoreError):
    """
    Raised when store is misconfigured or required components are missing.

    For example, when semantic search is requested but Qdrant is not enabled.
    """

    def __init__(self, message: str, original_error: Exception | None = None):
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
    """Configuration for secondary indexes (SAI)."""
    enable_sai: bool  # Enable Storage-Attached Indexes
    indexed_fields: list[str]  # Fields to create secondary indexes on


# Import LangChain Embeddings base class for type hints
try:
    from langchain_core.embeddings import Embeddings
except ImportError:
    Embeddings = None


class IndexConfig(TypedDict):
    """
    Configuration for semantic search with vector embeddings.

    Compatible with LangGraph's BaseStore IndexConfig.

    Fields:
        dims: Number of dimensions in embedding vectors (required)
        embed: Embedding function or provider (required)
        fields: Fields to extract for embedding (optional, defaults to ["$"])

    Example:
        >>> from langchain_google_genai import GoogleGenerativeAIEmbeddings
        >>>
        >>> embeddings = GoogleGenerativeAIEmbeddings(
        ...     model="models/gemini-embedding-001",
        ...     task_type="RETRIEVAL_DOCUMENT"
        ... )
        >>>
        >>> index_config: IndexConfig = {
        ...     "dims": 768,
        ...     "embed": embeddings,
        ...     "fields": ["text", "summary"]
        ... }
    """
    dims: int  # Required: embedding dimensions
    embed: Union[
        Any,  # Embeddings instance (use Any to avoid import requirement)
        Callable[[str], list[float]],                  # Sync embedding function
        Callable[[str], Awaitable[list[float]]],       # Async embedding function
        str                                             # Provider string (e.g., "openai:text-embedding-3-small")
    ]  # Required: embedding function or provider
    fields: list[str]  # Optional: fields to embed (defaults to ["$"])


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
        timeout_seconds: float = 60.0,
        alert_manager: Any | None = None
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            success_threshold: Number of successes needed to close circuit from half-open
            timeout_seconds: Time to wait before entering half-open state
            alert_manager: Optional AlertManager instance for critical alerts
        """
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout_seconds = timeout_seconds
        self.alert_manager = alert_manager

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

                    # Trigger critical alert for circuit breaker opening
                    if self.alert_manager:
                        self.alert_manager.trigger_alert(
                            severity=AlertSeverity.CRITICAL,
                            message=f"Circuit breaker OPEN: failure threshold {self.failure_threshold} exceeded",
                            context={"failure_count": self._failure_count, "state": "OPEN"}
                        )

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
        index: IndexConfig | None = None,
        sai_index: ScyllaIndexConfig | None = None,
        ttl: TTLConfig | None = None,
        execution_profiles: dict[str, ExecutionProfileConfig] | None = None,
        enable_circuit_breaker: bool = True,
        circuit_breaker_config: dict[str, Any] | None = None,
        enable_rate_limiting: bool = False,
        rate_limit_config: dict[str, Any] | None = None,
        enable_tracing: bool = False,
        enable_alerting: bool = False,
        qdrant_url: str = "http://localhost:6333",
        qdrant_collection: str | None = None,
        query_timeout: float = 10.0,
        qdrant_timeout: float = 5.0,
    ) -> None:
        """
        Initialize ScyllaDB store with Qdrant (REQUIRED for semantic search).

        Args:
            session: ScyllaDB/Cassandra session
            keyspace: Keyspace name
            deserializer: Optional JSON deserializer
            index: Semantic search configuration (REQUIRED - IndexConfig with dims and embed)
            sai_index: Optional SAI secondary index configuration
            ttl: Optional TTL configuration
            execution_profiles: Optional execution profile configurations
            enable_circuit_breaker: Enable circuit breaker for resilience (default: True)
            circuit_breaker_config: Circuit breaker configuration
            enable_rate_limiting: Enable rate limiting (default: False)
            rate_limit_config: Rate limiter configuration (requests_per_second, burst_size)
            enable_tracing: Enable OpenTelemetry tracing (default: False)
            enable_alerting: Enable alert manager (default: False)
            qdrant_url: Qdrant server URL (REQUIRED, default: http://localhost:6333)
            qdrant_collection: Qdrant collection name (default: keyspace name)
            query_timeout: Timeout for ScyllaDB queries in seconds (default: 10.0)
            qdrant_timeout: Timeout for Qdrant operations in seconds (default: 5.0)
        """
        # Validate keyspace name to prevent CQL injection
        self._validate_keyspace(keyspace)

        self.session = session
        self.keyspace = keyspace
        self.deserializer = deserializer or json.loads
        self.index_config = self._process_index_config(index) if index else None
        self.sai_config = sai_index or {}
        self.ttl_config = ttl or {}
        self.execution_profiles = execution_profiles or {}
        self.query_timeout = query_timeout
        self.qdrant_timeout = qdrant_timeout
        self.lock = asyncio.Lock()
        self._ttl_sweeper_task: asyncio.Task | None = None
        self._ttl_stop_event = asyncio.Event()

        # Prepared statements cache - will be populated during setup()
        self._prepared_statements: dict[str, Any] = {}

        # Vector index availability flag - set during setup()
        self._has_vector_index = False

        # Embedding cache for performance (LRU for better hit rate)
        self._embedding_cache = LRUCache(max_size=10000)

        # Enhanced Metrics tracking with percentiles (p50, p95, p99)
        self.metrics = EnhancedMetrics(service_name=f"scylladb_store_{keyspace}")
        logger.info("Using EnhancedMetrics with Prometheus support and percentile tracking")

        # OpenTelemetry Tracing (optional)
        if enable_tracing:
            self.tracer = Tracer(service_name=f"scylladb_store_{keyspace}")
            logger.info("OpenTelemetry tracing enabled")
        else:
            self.tracer = None

        # Alert Manager (optional)
        if enable_alerting:
            self.alert_manager = AlertManager()
            logger.info("Alert manager enabled")
        else:
            self.alert_manager = None

        # Rate Limiter (optional, disabled by default)
        if enable_rate_limiting:
            rl_config = rate_limit_config or {}
            self.rate_limiter = TokenBucketRateLimiter(
                default_requests_per_second=rl_config.get("requests_per_second", 1000),
                default_burst_size=rl_config.get("burst_size", 100)
            )
            logger.info(
                f"Rate limiting enabled: {rl_config.get('requests_per_second', 1000)} req/s, "
                f"burst={rl_config.get('burst_size', 100)}"
            )
        else:
            self.rate_limiter = None

        # Circuit breaker for resilience (enabled by default)
        if enable_circuit_breaker:
            cb_config = circuit_breaker_config or {}
            self.circuit_breaker = CircuitBreaker(
                failure_threshold=cb_config.get("failure_threshold", 5),
                success_threshold=cb_config.get("success_threshold", 2),
                timeout_seconds=cb_config.get("timeout_seconds", 60.0),
                alert_manager=self.alert_manager
            )
            logger.info(
                f"Circuit breaker enabled with defaults: "
                f"failure_threshold={self.circuit_breaker.failure_threshold}, "
                f"timeout={self.circuit_breaker.timeout_seconds}s"
            )
        else:
            self.circuit_breaker = None
            logger.info("Circuit breaker disabled")

        # Qdrant integration (MANDATORY for semantic search)
        if not QDRANT_AVAILABLE:
            raise StoreConfigurationError(
                "Qdrant is required but qdrant-client is not installed. "
                "Install with: pip install qdrant-client tenacity"
            )

        self.qdrant_client = AsyncQdrantClient(url=qdrant_url)
        self.qdrant_collection = qdrant_collection or keyspace
        logger.info(f"Qdrant client initialized (REQUIRED): {qdrant_url}, collection: {self.qdrant_collection}")

    def _process_index_config(self, config: IndexConfig) -> IndexConfig:
        """
        Validate and process IndexConfig.

        Args:
            config: Raw IndexConfig from user

        Returns:
            Processed IndexConfig

        Raises:
            ValueError: If config is invalid
        """
        if "dims" not in config:
            raise ValueError("IndexConfig must include 'dims' field")

        if "embed" not in config:
            raise ValueError("IndexConfig must include 'embed' field")

        # Set default fields if not provided
        if "fields" not in config:
            config["fields"] = ["$"]  # Default to embedding entire object

        # Validate dims
        dims = config["dims"]
        if not isinstance(dims, int) or dims <= 0:
            raise ValueError(f"IndexConfig 'dims' must be a positive integer, got {dims}")

        # Validate embed type
        embed = config["embed"]
        if isinstance(embed, str):
            # Provider string - try to convert using init_embeddings
            try:
                from langchain.embeddings import init_embeddings
                config["embed"] = init_embeddings(embed)
                logger.info(f"Initialized embeddings from provider string: {embed}")
            except ImportError:
                raise ValueError(
                    f"Cannot use provider string '{embed}' - langchain.embeddings not available. "
                    "Install with: pip install langchain"
                )
            except Exception as e:
                raise ValueError(f"Failed to initialize embeddings from provider '{embed}': {e}")
        elif not callable(embed) and not hasattr(embed, 'embed_query'):
            raise ValueError(
                "IndexConfig 'embed' must be a callable, Embeddings instance, or provider string"
            )

        logger.info(
            f"IndexConfig processed: dims={config['dims']}, "
            f"fields={config['fields']}, "
            f"embed_type={type(config['embed']).__name__}"
        )

        return config

    def _extract_fields_for_embedding(
        self,
        value: dict[str, Any],
        fields: list[str]
    ) -> str:
        """
        Extract specified fields from value dict for embedding.

        Supports JSON path syntax for nested fields.

        Args:
            value: Dictionary containing the data
            fields: List of field names (["$"] for all fields)

        Returns:
            Concatenated string of field values
        """
        if fields == ["$"]:
            # Embed entire object
            return json.dumps(value, ensure_ascii=False)

        # Extract specific fields
        parts = []
        for field in fields:
            if "." in field:
                # Support nested field access (e.g., "user.name")
                keys = field.split(".")
                val = value
                for key in keys:
                    if isinstance(val, dict) and key in val:
                        val = val[key]
                    else:
                        val = None
                        break
                if val is not None:
                    parts.append(str(val))
            elif field in value:
                parts.append(str(value[field]))

        return " ".join(parts)

    async def _with_qdrant_timeout(self, coro, operation: str = "qdrant_operation"):
        """
        Execute Qdrant operation with timeout.

        Args:
            coro: Coroutine to execute
            operation: Operation name for error messages

        Returns:
            Result from coroutine

        Raises:
            StoreTimeoutError: If operation times out
        """
        try:
            return await asyncio.wait_for(coro, timeout=self.qdrant_timeout)
        except asyncio.TimeoutError:
            error_msg = f"Qdrant {operation} timed out after {self.qdrant_timeout}s"
            logger.error(error_msg)
            raise StoreTimeoutError(
                error_msg,
                operation=operation,
                timeout_seconds=self.qdrant_timeout
            )

    async def _generate_embedding(
        self,
        value: dict[str, Any],
    ) -> list[float] | None:
        """
        Generate embedding for a value dict using IndexConfig with caching.

        Args:
            value: Data to embed

        Returns:
            Embedding vector or None if embedding fails
        """
        if not self.index_config:
            return None

        # Extract fields
        text = self._extract_fields_for_embedding(
            value,
            self.index_config.get("fields", ["$"])
        )

        if not text.strip():
            logger.warning("Empty text for embedding, skipping")
            return None

        # Check cache (hash-based key)
        import hashlib
        cache_key = hashlib.md5(text.encode()).hexdigest()

        cached_embedding = self._embedding_cache.get(cache_key)
        if cached_embedding is not None:
            logger.debug(f"Embedding cache hit for key {cache_key[:8]}...")
            return cached_embedding

        # Generate embedding
        try:
            embed = self.index_config["embed"]

            # Handle different embedding types
            if hasattr(embed, 'aembed_query'):
                # LangChain Embeddings with async support (faster)
                embedding = await embed.aembed_query(text)
            elif hasattr(embed, 'embed_query'):
                # LangChain Embeddings instance (sync - run in executor)
                loop = asyncio.get_running_loop()
                embedding = await loop.run_in_executor(None, embed.embed_query, text)
            elif asyncio.iscoroutinefunction(embed):
                # Async function
                embedding = await embed(text)
            elif callable(embed):
                # Sync function - run in executor
                loop = asyncio.get_running_loop()
                embedding = await loop.run_in_executor(None, embed, text)
            else:
                raise ValueError(f"Unsupported embedding type: {type(embed)}")

            # Validate dimensions
            expected_dims = self.index_config["dims"]
            if len(embedding) != expected_dims:
                raise ValueError(
                    f"Embedding dimension mismatch: expected {expected_dims}, got {len(embedding)}"
                )

            # Cache the embedding (LRU eviction)
            self._embedding_cache.put(cache_key, embedding)
            logger.debug(f"Cached embedding for key {cache_key[:8]}... (LRU cache)")

            return embedding

        except Exception as e:
            # Log error but don't fail the operation
            logger.error(f"Failed to generate embedding: {e}", exc_info=e)
            return None

    async def _generate_embeddings_batch(
        self,
        values: list[dict[str, Any]],
        max_batch_size: int = 100,
        max_parallel_batches: int = 5
    ) -> list[list[float] | None]:
        """
        Generate embeddings for multiple documents with optimized batching and parallelization.

        Strategy for massive scale:
        1. Check cache first to avoid redundant API calls
        2. Split into optimal batch sizes (Gemini API recommends ≤100 per batch)
        3. Process multiple batches in parallel to maximize throughput
        4. Cache results aggressively for reuse

        Args:
            values: List of document values to embed
            max_batch_size: Maximum documents per batch (default 100 for Gemini)
            max_parallel_batches: Maximum parallel batches to process (default 5)

        Returns:
            List of embedding vectors (or None for skipped documents)
        """
        if not self.index_config or not values:
            return [None] * len(values)

        import hashlib

        # Extract texts and check cache
        texts = []
        cache_keys = []
        embeddings_result = [None] * len(values)
        uncached_indices = []

        for i, value in enumerate(values):
            text = self._extract_fields_for_embedding(
                value,
                self.index_config.get("fields", ["$"])
            )
            if not text or not text.strip():
                continue

            cache_key = hashlib.md5(text.encode()).hexdigest()
            cached_embedding = self._embedding_cache.get(cache_key)

            if cached_embedding is not None:
                embeddings_result[i] = cached_embedding
                logger.debug(f"Embedding cache hit for key {cache_key[:8]}...")
            else:
                texts.append(text)
                cache_keys.append(cache_key)
                uncached_indices.append(i)

        # All embeddings were cached
        if not texts:
            cache_hits = len([e for e in embeddings_result if e is not None])
            logger.info(f"Batch embedding: {cache_hits}/{len(values)} from cache (100% hit rate)")
            return embeddings_result

        cache_hits = len(values) - len(texts)
        logger.info(f"Batch embedding: {cache_hits}/{len(values)} from cache, {len(texts)} to generate")

        # Split into optimal batch sizes for parallel processing
        async def generate_batch_chunk(chunk_texts: list[str], chunk_indices: list[int], chunk_keys: list[str]):
            """Generate embeddings for a single chunk."""
            try:
                embed = self.index_config["embed"]

                # Use batch embedding API
                if hasattr(embed, 'aembed_documents'):
                    embeddings = await embed.aembed_documents(chunk_texts)
                elif hasattr(embed, 'embed_documents'):
                    loop = asyncio.get_running_loop()
                    embeddings = await loop.run_in_executor(None, embed.embed_documents, chunk_texts)
                else:
                    # No batch support - process individually
                    embeddings = []
                    for text in chunk_texts:
                        if hasattr(embed, 'aembed_query'):
                            emb = await embed.aembed_query(text)
                        elif hasattr(embed, 'embed_query'):
                            loop = asyncio.get_running_loop()
                            emb = await loop.run_in_executor(None, embed.embed_query, text)
                        elif asyncio.iscoroutinefunction(embed):
                            emb = await embed(text)
                        elif callable(embed):
                            loop = asyncio.get_running_loop()
                            emb = await loop.run_in_executor(None, embed, text)
                        else:
                            raise ValueError(f"Unsupported embedding type: {type(embed)}")
                        embeddings.append(emb)

                return embeddings, chunk_indices, chunk_keys

            except Exception as e:
                logger.error(f"Batch chunk failed: {e}", exc_info=e)
                return None, chunk_indices, chunk_keys

        # Create batches for parallel processing
        batches = []
        for i in range(0, len(texts), max_batch_size):
            end_idx = min(i + max_batch_size, len(texts))
            batches.append((
                texts[i:end_idx],
                uncached_indices[i:end_idx],
                cache_keys[i:end_idx]
            ))

        # Process batches in parallel (with concurrency limit)
        tasks = []
        for batch_texts, batch_indices, batch_keys in batches:
            task = generate_batch_chunk(batch_texts, batch_indices, batch_keys)
            tasks.append(task)

        # Process with limited parallelism to avoid overwhelming the API
        results = []
        for i in range(0, len(tasks), max_parallel_batches):
            batch_tasks = tasks[i:i + max_parallel_batches]
            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            results.extend(batch_results)

        # Validate and cache results
        expected_dims = self.index_config["dims"]
        total_generated = 0

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Batch generation failed: {result}")
                continue

            embeddings, chunk_indices, chunk_keys = result
            if embeddings is None:
                continue

            for embedding, cache_key, original_idx in zip(embeddings, chunk_keys, chunk_indices):
                if len(embedding) != expected_dims:
                    logger.warning(
                        f"Embedding dimension mismatch: expected {expected_dims}, got {len(embedding)}"
                    )
                    continue

                # Cache and store result
                self._embedding_cache.put(cache_key, embedding)
                embeddings_result[original_idx] = embedding
                total_generated += 1

        logger.info(
            f"Generated {total_generated} embeddings in {len(batches)} batch(es) "
            f"({max_parallel_batches} parallel), cache hit rate: {cache_hits}/{len(values)} "
            f"({100 * cache_hits / len(values):.1f}%)"
        )

        return embeddings_result

    @classmethod
    @asynccontextmanager
    async def from_contact_points(
        cls,
        contact_points: list[str],
        keyspace: str,
        *,
        pool_config: PoolConfig | None = None,
        index: IndexConfig | None = None,
        sai_index: ScyllaIndexConfig | None = None,
        ttl: TTLConfig | None = None,
        execution_profiles: dict[str, ExecutionProfileConfig] | None = None,
        enable_circuit_breaker: bool = True,
        circuit_breaker_config: dict[str, Any] | None = None,
        enable_rate_limiting: bool = False,
        rate_limit_config: dict[str, Any] | None = None,
        enable_tracing: bool = False,
        enable_alerting: bool = False,
        qdrant_url: str = "http://localhost:6333",
        qdrant_collection: str | None = None,
    ) -> AsyncIterator["AsyncScyllaDBStore"]:
        """
        Create AsyncScyllaDBStore from contact points with Qdrant (REQUIRED).

        Args:
            contact_points: List of ScyllaDB node addresses
            keyspace: Keyspace name
            pool_config: Optional connection pool configuration
            index: Index configuration (REQUIRED for semantic search)
            ttl: Optional TTL configuration
            execution_profiles: Optional execution profile configurations
            qdrant_url: Qdrant server URL (REQUIRED, default: http://localhost:6333)
            qdrant_collection: Qdrant collection name (default: keyspace)

        Yields:
            AsyncScyllaDBStore instance

        Example:
            async with AsyncScyllaDBStore.from_contact_points(
                contact_points=["127.0.0.1"],
                keyspace="my_store",
                index={"dims": 768, "embed": embeddings},
                qdrant_url="http://localhost:6333"
            ) as store:
                await store.setup()
                # Use store...
        """
        pool_config = pool_config or {}

        # Apply best practice defaults if not specified
        cluster_config = {
            "contact_points": contact_points,
            "connection_class": AsyncioConnection,
            "port": pool_config.get("port", 9042),
        }

        # Create default execution profile with ScyllaDB-optimized settings
        # Use ScyllaDB-optimized TokenAwarePolicy for shard awareness
        # This reduces latency by routing requests directly to the right shard
        load_balancing_policy = pool_config.get(
            "load_balancing_policy",
            TokenAwarePolicy(DCAwareRoundRobinPolicy())
        )

        default_profile = ExecutionProfile(
            load_balancing_policy=load_balancing_policy,
            request_timeout=pool_config.get("request_timeout", 10.0),
        )

        cluster_config["execution_profiles"] = {EXEC_PROFILE_DEFAULT: default_profile}
        logger.info("Using TokenAwarePolicy for ScyllaDB shard-aware routing via execution profile")

        # Create cluster with optimized configuration
        cluster = Cluster(**cluster_config)

        logger.info("Created cluster with execution profile configuration")

        # Add custom execution profiles if configured
        if execution_profiles:
            for profile_name, profile_config in execution_profiles.items():
                profile = ExecutionProfile(**profile_config)
                cluster.add_execution_profile(profile_name, profile)
                logger.info(f"Added custom execution profile: {profile_name}")

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
                sai_index=sai_index,
                ttl=ttl,
                execution_profiles=execution_profiles,
                enable_circuit_breaker=enable_circuit_breaker,
                circuit_breaker_config=circuit_breaker_config,
                enable_rate_limiting=enable_rate_limiting,
                rate_limit_config=rate_limit_config,
                enable_tracing=enable_tracing,
                enable_alerting=enable_alerting,
                qdrant_url=qdrant_url,
                qdrant_collection=qdrant_collection,
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
        # Qdrant is REQUIRED for semantic search - embeddings are stored ONLY in Qdrant
        # ScyllaDB stores documents and metadata only (no embeddings)
        await self._execute_async("""
            CREATE TABLE IF NOT EXISTS store (
                prefix text,
                key text,
                value text,
                created_at timestamp,
                updated_at timestamp,
                ttl_seconds int,
                PRIMARY KEY (prefix, key)
            )
        """)

        logger.info("Created store table (documents only - vectors in Qdrant)")

        # No vector index needed in ScyllaDB - Qdrant handles all vector operations
        self._has_vector_index = False

        # Note: prefix is the partition key, so no secondary index needed
        # WHERE prefix = ? queries are already efficient

        # Note: No need for secondary index on prefix - it's already the partition key!
        # Partition key queries (WHERE prefix = ?) are automatically optimized by ScyllaDB
        # For prefix range queries (WHERE prefix >= ? AND prefix < ?), we use ALLOW FILTERING
        # which is efficient when the result set is small relative to total data
        logger.info("Using partition key (prefix) for efficient queries - no secondary index needed")

        # Create SAI secondary indexes if configured
        if self.sai_config.get("enable_sai"):
            indexed_fields = self.sai_config.get("indexed_fields", [])
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

        # Setup Qdrant collection (REQUIRED)
        embedding_dims = self.index_config.get("dims") if self.index_config else None
        if embedding_dims:
            await self._setup_qdrant_collection(embedding_dims)
        else:
            logger.warning("No embedding dimensions configured - semantic search will fail. Configure index_config with 'dims' and 'embed'.")

        logger.info(f"ScyllaDB store setup complete in keyspace '{self.keyspace}'")

    async def _setup_qdrant_collection(self, dims: int) -> None:
        """
        Create Qdrant collection for vector storage if it doesn't exist.

        Args:
            dims: Embedding dimensions
        """
        if not self.qdrant_client:
            return

        try:
            # Check if collection exists
            collections = await self.qdrant_client.get_collections()
            collection_names = [c.name for c in collections.collections]

            if self.qdrant_collection not in collection_names:
                # Create collection with cosine distance
                await self.qdrant_client.create_collection(
                    collection_name=self.qdrant_collection,
                    vectors_config=VectorParams(size=dims, distance=Distance.COSINE),
                )
                logger.info(f"Created Qdrant collection '{self.qdrant_collection}' with {dims} dimensions")
            else:
                logger.info(f"Qdrant collection '{self.qdrant_collection}' already exists")
        except Exception as e:
            logger.error(f"Failed to setup Qdrant collection: {e}")
            raise StoreConfigurationError("Qdrant is required but collection setup failed", e)

    async def _sync_to_qdrant(
        self,
        point_id: str,
        embedding: list[float],
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
    ) -> None:
        """
        Sync vector and metadata to Qdrant with retry logic.

        Uses exponential backoff to handle transient failures and maintain
        consistency between ScyllaDB and Qdrant.

        Args:
            point_id: Unique point ID (combination of namespace and key)
            embedding: Vector embedding
            namespace: Item namespace
            key: Item key
            value: Item value (for metadata extraction)
        """
        # Qdrant is always available - no check needed

        # Create point with vector and payload
        point = PointStruct(
            id=point_id,
            vector=embedding,
            payload={
                "namespace": list(namespace),
                "key": key,
                "scylla_id": f"{'.'.join(namespace)}.{key}",
                # Add searchable metadata from value
                **{k: v for k, v in value.items() if isinstance(v, (str, int, float, bool))}
            },
        )

        # Retry with exponential backoff
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self.qdrant_client.upsert(
                    collection_name=self.qdrant_collection,
                    points=[point],
                )
                logger.debug(f"Synced vector to Qdrant: {point_id}")
                return  # Success
            except Exception as e:
                if attempt < max_retries - 1:
                    # Exponential backoff: 1s, 2s, 4s
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Qdrant sync attempt {attempt + 1}/{max_retries} failed: {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # Final attempt failed - raise error to prevent silent data inconsistency
                    error_msg = (
                        f"Failed to sync to Qdrant after {max_retries} attempts: {e}. "
                        f"Data inconsistency detected for point {point_id}. "
                        f"ScyllaDB write succeeded but vector sync failed."
                    )
                    logger.error(error_msg)

                    # Trigger critical alert for data inconsistency
                    if self.alert_manager:
                        self.alert_manager.trigger_alert(
                            severity=AlertSeverity.CRITICAL,
                            message="Data inconsistency: Qdrant sync failed after ScyllaDB write",
                            context={"point_id": point_id, "error": str(e), "max_retries": max_retries}
                        )

                    raise StoreQueryError(error_msg, original_error=e)

    async def _batch_sync_to_qdrant(
        self,
        items: list[tuple[str, list[float], tuple[str, ...], str, dict[str, Any]]]
        # [(point_id, embedding, namespace, key, value), ...]
    ) -> None:
        """
        Batch sync multiple vectors to Qdrant for better performance.

        Instead of individual upserts, batches multiple points into a single
        request, reducing network overhead by ~10x.

        Args:
            items: List of (point_id, embedding, namespace, key, value) tuples
        """
        if not items:
            return

        # Create batch of points
        points = []
        for point_id, embedding, namespace, key, value in items:
            point = PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "namespace": list(namespace),
                    "key": key,
                    "scylla_id": f"{'.'.join(namespace)}.{key}",
                    **{k: v for k, v in value.items() if isinstance(v, (str, int, float, bool))}
                },
            )
            points.append(point)

        # Batch upsert with retry
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self.qdrant_client.upsert(
                    collection_name=self.qdrant_collection,
                    points=points,
                )
                logger.debug(f"Batch synced {len(points)} vectors to Qdrant")
                return  # Success
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Qdrant batch sync attempt {attempt + 1}/{max_retries} failed: {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # Final attempt failed - raise error to prevent silent data inconsistency
                    error_msg = (
                        f"Failed to batch sync {len(points)} points to Qdrant after {max_retries} attempts: {e}. "
                        f"Data inconsistency detected. ScyllaDB writes succeeded but vector sync failed."
                    )
                    logger.error(error_msg)

                    # Trigger critical alert for batch data inconsistency
                    if self.alert_manager:
                        self.alert_manager.trigger_alert(
                            severity=AlertSeverity.CRITICAL,
                            message=f"Batch data inconsistency: {len(points)} Qdrant syncs failed",
                            context={"point_count": len(points), "error": str(e), "max_retries": max_retries}
                        )

                    raise StoreQueryError(error_msg, original_error=e)

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
            timeout_seconds=timeout_seconds,
            alert_manager=self.alert_manager
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
        # Rate limiting check
        if self.rate_limiter:
            if not self.rate_limiter.check("default"):
                retry_after = self.rate_limiter.get_retry_after("default")
                raise RateLimitExceeded("GET operation rate limit exceeded", retry_after)

        # OpenTelemetry tracing
        if self.tracer:
            async with self.tracer.span("aget", attributes={"namespace": str(namespace), "key": key}):
                return await self._aget_impl(namespace, key, refresh_ttl=refresh_ttl)
        else:
            return await self._aget_impl(namespace, key, refresh_ttl=refresh_ttl)

    async def _aget_impl(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        refresh_ttl: bool | None = None,
    ) -> Item | None:
        """Internal implementation of aget."""
        # Validate inputs
        self._validate_namespace(namespace)
        self._validate_key(key)

        prefix = self._namespace_to_prefix(namespace)
        should_refresh = refresh_ttl if refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

        # Use pre-prepared statement
        result = await self._execute_prepared("get", (prefix, key))

        if not result:
            return None

        row = result.one() if hasattr(result, 'one') else (result[0] if result else None)
        if not row:
            return None

        # If value is None, the row expired (TTL reached)
        if row.value is None:
            return None

        # Refresh TTL if requested and TTL is set
        if should_refresh and row.ttl_seconds:
            await self._refresh_ttl(prefix, key, row.value, row.ttl_seconds)

        return self._row_to_item(row, namespace, key)

    async def aput(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        index: Literal[False] | list[str] | None = None,
        *,
        ttl: float | None | type[NOT_PROVIDED] = NOT_PROVIDED,
        wait_for_vector_sync: bool = False,
    ) -> None:
        """
        Store or update an item asynchronously.

        Args:
            namespace: Hierarchical path
            key: Unique identifier
            value: Dictionary to store (must be JSON-serializable)
            index: Controls field indexing (not fully implemented for ScyllaDB)
            ttl: Time-to-live in minutes (None = no expiration)
            wait_for_vector_sync: If True, wait for Qdrant vector sync to complete before
                                  returning, ensuring documents are immediately searchable.
                                  Default is False for maximum write throughput (eventual
                                  consistency). Set to True when you need to search immediately
                                  after writing (e.g., tests, interactive demos).

        Raises:
            StoreValidationError: If inputs are invalid
            StoreQueryError: If write fails
            StoreTimeoutError: If operation times out
            StoreUnavailableError: If required replicas are unavailable
        """
        # Rate limiting check
        if self.rate_limiter:
            if not self.rate_limiter.check("default"):
                retry_after = self.rate_limiter.get_retry_after("default")
                raise RateLimitExceeded("PUT operation rate limit exceeded", retry_after)

        # OpenTelemetry tracing
        if self.tracer:
            async with self.tracer.span("aput", attributes={"namespace": str(namespace), "key": key}):
                return await self._aput_impl(namespace, key, value, index, ttl=ttl, wait_for_vector_sync=wait_for_vector_sync)
        else:
            return await self._aput_impl(namespace, key, value, index, ttl=ttl, wait_for_vector_sync=wait_for_vector_sync)

    async def _aput_impl(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        index: Literal[False] | list[str] | None = None,
        *,
        ttl: float | None | type[NOT_PROVIDED] = NOT_PROVIDED,
        wait_for_vector_sync: bool = False,
    ) -> None:
        """Internal implementation of aput."""
        # Validate all inputs
        self._validate_namespace(namespace)
        self._validate_key(key)
        self._validate_value(value)

        prefix = self._namespace_to_prefix(namespace)
        value_json = json.dumps(value)
        now = datetime.now(timezone.utc)

        # Determine TTL (all in seconds for precision)
        ttl_seconds = None

        if ttl is not NOT_PROVIDED:
            if ttl is not None:
                ttl_seconds = int(ttl)  # ttl parameter is in seconds
        elif self.ttl_config.get("default_ttl"):
            ttl_seconds = int(self.ttl_config["default_ttl"])

        # Generate embedding if IndexConfig is set and indexing not disabled
        embedding = None
        if self.index_config and index is not False:
            # Override fields if index parameter provides specific fields
            if isinstance(index, list):
                # Temporarily override fields for this operation
                original_fields = self.index_config.get("fields")
                self.index_config["fields"] = index
                embedding = await self._generate_embedding(value)
                self.index_config["fields"] = original_fields
            else:
                embedding = await self._generate_embedding(value)

        # Store document data only (no embeddings in ScyllaDB - always in Qdrant)
        if ttl_seconds:
            await self._execute_prepared(
                "put_with_ttl",
                (prefix, key, value_json, now, now, ttl_seconds, ttl_seconds)
            )
        else:
            await self._execute_prepared(
                "put_no_ttl",
                (prefix, key, value_json, now, now, None)
            )

        # Sync to Qdrant if embedding was generated
        if embedding:
            import hashlib
            # Use hash of prefix+key as point ID (Qdrant requires UUID or integer)
            point_id = hashlib.md5(f"{prefix}.{key}".encode()).hexdigest()

            if wait_for_vector_sync:
                # Wait for sync to complete (immediate search consistency)
                await self._sync_to_qdrant(point_id, embedding, namespace, key, value)
            else:
                # Fire-and-forget for speed (eventual consistency)
                asyncio.create_task(self._sync_to_qdrant(point_id, embedding, namespace, key, value))

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
        # Rate limiting check
        if self.rate_limiter:
            if not self.rate_limiter.check("default"):
                retry_after = self.rate_limiter.get_retry_after("default")
                raise RateLimitExceeded("DELETE operation rate limit exceeded", retry_after)

        # OpenTelemetry tracing
        if self.tracer:
            async with self.tracer.span("adelete", attributes={"namespace": str(namespace), "key": key}):
                return await self._adelete_impl(namespace, key)
        else:
            return await self._adelete_impl(namespace, key)

    async def _adelete_impl(
        self,
        namespace: tuple[str, ...],
        key: str,
    ) -> None:
        """Internal implementation of adelete."""
        self._validate_namespace(namespace)

        prefix = self._namespace_to_prefix(namespace)
        await self._execute_prepared("delete", (prefix, key))

        # Remove from Qdrant (only if embeddings are configured)
        if self.index_config:
            try:
                import hashlib
                point_id = hashlib.md5(f"{prefix}.{key}".encode()).hexdigest()
                await self.qdrant_client.delete(
                    collection_name=self.qdrant_collection,
                    points_selector=[point_id],
                )
                logger.debug(f"Deleted vector from Qdrant: {point_id}")
            except Exception as e:
                logger.warning(f"Failed to delete from Qdrant: {e}")

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

        # Determine TTL (in seconds)
        ttl_seconds = None
        if ttl is not None:
            ttl_seconds = int(ttl)

        # Execute LWT
        if ttl_seconds:
            result = await self._execute_prepared(
                "put_if_not_exists_with_ttl",
                (prefix, key, value_json, now, now, ttl_seconds, ttl_seconds)
            )
        else:
            result = await self._execute_prepared(
                "put_if_not_exists_no_ttl",
                (prefix, key, value_json, now, now, None)
            )

        # Check [applied] column
        if not result:
            return False
        row = result.one() if hasattr(result, 'one') else (result[0] if result else None)
        return row.applied if row else False

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

        if not result:
            return False
        row = result.one() if hasattr(result, 'one') else (result[0] if result else None)
        return row.applied if row else False

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

        if not result:
            return False
        row = result.one() if hasattr(result, 'one') else (result[0] if result else None)
        return row.applied if row else False

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

        if not result:
            return False
        row = result.one() if hasattr(result, 'one') else (result[0] if result else None)
        return row.applied if row else False

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

        if not result:
            return False
        row = result.one() if hasattr(result, 'one') else (result[0] if result else None)
        return row.applied if row else False

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

        Supports both semantic search (when query provided and IndexConfig set)
        and filter-based search.

        Args:
            namespace_prefix: Path prefix to search within
            query: Natural language query for semantic search
            filter: Key-value pairs for filtering results
            limit: Maximum items to return
            offset: Number of items to skip (pagination)
            refresh_ttl: Whether to refresh TTL on read
            fetch_size: Page size for query paging (default: 5000)

        Returns:
            List of SearchItem objects sorted by relevance

        Note:
            - With query + IndexConfig: Returns results sorted by semantic similarity
            - Without query: Returns results sorted by recency
            - Filters are always applied regardless of search mode
        """
        # Rate limiting check
        if self.rate_limiter:
            if not self.rate_limiter.check("default"):
                retry_after = self.rate_limiter.get_retry_after("default")
                raise RateLimitExceeded("SEARCH operation rate limit exceeded", retry_after)

        # OpenTelemetry tracing
        if self.tracer:
            async with self.tracer.span(
                "asearch",
                attributes={
                    "namespace_prefix": str(namespace_prefix),
                    "query": query or "",
                    "limit": limit,
                    "offset": offset,
                }
            ):
                return await self._asearch_impl(
                    namespace_prefix, query, filter, limit, offset, refresh_ttl, fetch_size
                )
        else:
            return await self._asearch_impl(
                namespace_prefix, query, filter, limit, offset, refresh_ttl, fetch_size
            )

    async def _asearch_impl(
        self,
        namespace_prefix: tuple[str, ...],
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
        refresh_ttl: bool | None = None,
        fetch_size: int | None = None,
    ) -> list[SearchItem]:
        """Internal implementation of asearch."""
        self._validate_namespace(namespace_prefix)

        # If query provided and semantic search enabled, use semantic search
        if query and self.index_config:
            return await self._semantic_search(
                namespace_prefix, query, filter, limit, offset, refresh_ttl, fetch_size
            )

        # Otherwise, use filter-based search
        return await self._filter_search(
            namespace_prefix, filter, limit, offset, refresh_ttl, fetch_size
        )

    async def _filter_search(
        self,
        namespace_prefix: tuple[str, ...],
        filter: dict[str, Any] | None,
        limit: int,
        offset: int,
        refresh_ttl: bool | None,
        fetch_size: int | None,
    ) -> list[SearchItem]:
        """Filter-based search with prefix range query optimization (using SAI index)."""
        prefix = self._namespace_to_prefix(namespace_prefix)
        should_refresh = refresh_ttl if refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

        # Use prefix range query with SAI index for optimal performance
        # Instead of full table scan, use WHERE clause with prefix range
        # This requires the SAI index created in setup()
        prefix_end = prefix + "\uffff"  # Unicode max character for range upper bound

        query_str = f"""
            SELECT * FROM {self.keyspace}.store
            WHERE prefix >= ? AND prefix < ?
            ALLOW FILTERING
        """

        results = await self._execute_async(query_str, (prefix, prefix_end))

        if not results:
            return []

        items = []
        ttl_refresh_items = []  # Collect TTL refresh operations for batching

        for row in results:
            # No need to check prefix match - already filtered by WHERE clause
            value = json.loads(row.value)

            if filter and not self._matches_filter(value, filter):
                continue

            ns = self._prefix_to_namespace(row.prefix)

            # Collect TTL refresh operations instead of executing immediately
            if should_refresh and row.ttl_seconds:
                ttl_refresh_items.append((row.prefix, row.key, row.value, row.ttl_seconds))

            score = self._calculate_relevance_score(
                value=value,
                filter=filter,
                created_at=row.created_at,
                updated_at=row.updated_at,
                query=None
            )

            items.append(SearchItem(
                value=value,
                key=row.key,
                namespace=ns,
                created_at=row.created_at,
                updated_at=row.updated_at,
                score=score
            ))

        # Batch refresh TTLs for better performance (reduces write amplification)
        if ttl_refresh_items:
            await self._batch_refresh_ttls(ttl_refresh_items)

        return items[offset:offset + limit]

    async def _semantic_search(
        self,
        namespace_prefix: tuple[str, ...],
        query: str,
        filter: dict[str, Any] | None,
        limit: int,
        offset: int,
        refresh_ttl: bool | None,
        fetch_size: int | None,
    ) -> list[SearchItem]:
        """
        Semantic search using Qdrant vector store (REQUIRED).

        No fallback to sklearn - Qdrant must be enabled and available for semantic search.
        """
        # Qdrant is REQUIRED for semantic search (always available)
        if not self.qdrant_client:
            raise StoreConfigurationError(
                "Semantic search requires Qdrant client. This should not happen - check initialization."
            )

        should_refresh = refresh_ttl if refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

        # Generate query embedding
        query_embedding = await self._generate_query_embedding(query)
        if not query_embedding:
            raise StoreQueryError(
                "Failed to generate query embedding. Check embedding configuration and API access."
            )

        # Use Qdrant for all semantic search (no fallback)
        return await self._semantic_search_with_qdrant(
            namespace_prefix, query_embedding, filter, limit, offset, should_refresh
        )

    async def _semantic_search_with_qdrant(
        self,
        namespace_prefix: tuple[str, ...],
        query_embedding: list[float],
        filter: dict[str, Any] | None,
        limit: int,
        offset: int,
        should_refresh: bool,
    ) -> list[SearchItem]:
        """
        Semantic search using Qdrant for fast ANN queries.

        Args:
            namespace_prefix: Namespace prefix to filter by
            query_embedding: Query vector
            filter: Additional metadata filters
            limit: Maximum results to return
            offset: Number of results to skip
            should_refresh: Whether to refresh TTL on read

        Returns:
            List of search results with similarity scores
        """
        if not self.qdrant_client:
            raise ValueError("Qdrant client not initialized")

        # Search Qdrant with dynamic limit (buffer for client-side filtering)
        # Fetch extra to account for namespace filtering and offset
        fetch_limit = min((limit + offset) * 2 + 20, 1000)

        search_results = await self._with_qdrant_timeout(
            self.qdrant_client.query_points(
                collection_name=self.qdrant_collection,
                query=query_embedding,
                limit=fetch_limit,
                with_payload=True,
            ),
            operation="search"
        )

        # Filter by namespace prefix client-side and collect keys for batch fetch
        prefix_str = ".".join(namespace_prefix)
        keys_to_fetch = []
        result_map = {}  # Map (namespace, key) -> (score, result)

        # Handle query_points response (returns points attribute)
        points = search_results.points if hasattr(search_results, 'points') else search_results

        for result in points:
            payload = result.payload
            scylla_id = payload.get("scylla_id", "")

            # Check if scylla_id matches namespace prefix
            if namespace_prefix and not scylla_id.startswith(prefix_str + "."):
                continue

            namespace_list = payload.get("namespace", [])
            key = payload.get("key", "")
            namespace = tuple(namespace_list)

            # Store for batch fetching
            key_tuple = (namespace, key)
            keys_to_fetch.append(key_tuple)
            result_map[key_tuple] = (float(result.score), result)

        if not keys_to_fetch:
            num_results = len(points) if points else 0
            logger.debug(f"No keys to fetch after Qdrant search (searched {num_results} results)")
            return []

        logger.debug(f"Batch fetching {len(keys_to_fetch)} items from ScyllaDB")

        # Use optimized batch fetch (fixes N+1 query pattern - ~10x faster)
        fetched_items = await self._batch_fetch_items(keys_to_fetch)

        # Log any None items for debugging
        none_count = sum(1 for item in fetched_items if item is None)
        if none_count > 0:
            logger.debug(f"Batch fetch: {none_count}/{len(fetched_items)} items not found")

        # Process results and apply filters
        items = []
        ttl_refresh_tasks = []

        for key_tuple, item in zip(keys_to_fetch, fetched_items):
            # Skip None items (not found)
            if item is None:
                logger.debug(f"Skipping not found item: {key_tuple}")
                continue

            # Apply metadata filter if specified
            if filter and not self._matches_filter(item.value, filter):
                continue

            # Get score from result map
            score, _ = result_map[key_tuple]

            # Create SearchItem
            search_item = SearchItem(
                value=item.value,
                key=item.key,
                namespace=item.namespace,
                created_at=item.created_at,
                updated_at=item.updated_at,
                score=score,
            )
            items.append(search_item)

            # Note: TTL refresh would require fetching row.ttl_seconds
            # Since Item doesn't include ttl_seconds, we skip TTL refresh in batch mode
            # Individual aget() calls handle TTL refresh when needed

        # Fire-and-forget TTL refreshes (don't block response)
        if ttl_refresh_tasks:
            asyncio.create_task(asyncio.gather(*ttl_refresh_tasks, return_exceptions=True))

        # Apply offset and limit after client-side filtering
        return items[offset:offset + limit]

    async def _generate_query_embedding(self, query: str) -> list[float] | None:
        """
        Generate embedding for a search query.

        Uses a separate RETRIEVAL_QUERY task type if the embedding supports it.
        """
        if not self.index_config:
            return None

        try:
            embed = self.index_config["embed"]

            # Check if it's a LangChain Embeddings instance
            if hasattr(embed, 'embed_query'):
                # Use embed_query for queries (may use different task type)
                return embed.embed_query(query)
            elif asyncio.iscoroutinefunction(embed):
                return await embed(query)
            elif callable(embed):
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, embed, query)
            else:
                raise ValueError(f"Unsupported embedding type: {type(embed)}")

        except Exception as e:
            logger.error(f"Failed to generate query embedding: {e}", exc_info=e)
            return None

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

        # Auto-chunk if batch exceeds maximum size
        if len(ops_list) > ValidationLimits.MAX_BATCH_SIZE:
            logger.info(
                f"Auto-chunking batch of {len(ops_list)} operations into chunks of "
                f"{ValidationLimits.MAX_BATCH_SIZE}"
            )

            all_results = []
            for i in range(0, len(ops_list), ValidationLimits.MAX_BATCH_SIZE):
                chunk = ops_list[i:i + ValidationLimits.MAX_BATCH_SIZE]

                # Execute chunk with retry logic (recursively call abatch, which won't chunk again)
                chunk_results = await self.abatch(
                    chunk,
                    batch_type=batch_type,
                    max_retries=max_retries,
                    retry_delay=retry_delay,
                    retry_backoff=retry_backoff
                )
                all_results.extend(chunk_results)

            return all_results

        # Warn for large batches (but within limit)
        if len(ops_list) > ValidationLimits.WARN_BATCH_SIZE:
            logger.warning(
                f"Large batch detected ({len(ops_list)} operations). "
                f"Performance may be better with smaller batches."
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
                self.metrics.record_batch(
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
                self.metrics.record_batch(
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
            self.metrics.record_batch(
                batch_size=len(ops_list),
                batch_type="concurrent",
                latency_ms=latency_ms,
                success=True
            )
        except Exception as e:
            # Record failure
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_batch(
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
        return self.metrics.get_stats()

    async def reset_metrics(self):
        """
        Reset all performance metrics counters.

        Useful for starting fresh measurements or after deployment.
        """
        self.metrics.reset()
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
            metrics = self.metrics.get_stats()
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
        metrics = self.metrics.get_stats()

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

        # Log final cache statistics
        if self.index_config and self._embedding_cache:
            stats = self.get_embedding_cache_stats()
            logger.info(
                f"Embedding cache final stats: {stats['hits']}/{stats['hits'] + stats['misses']} hits "
                f"({stats['hit_rate']:.1%}), size: {stats['size']}/{stats['max_size']}"
            )

    def get_embedding_cache_stats(self) -> dict[str, Any]:
        """
        Get embedding cache performance statistics.

        Returns:
            Dictionary with cache metrics:
            - size: Current number of cached embeddings
            - max_size: Maximum cache capacity
            - hits: Number of cache hits
            - misses: Number of cache misses
            - hit_rate: Cache hit rate (0.0 to 1.0)

        Example:
            >>> stats = store.get_embedding_cache_stats()
            >>> print(f"Cache hit rate: {stats['hit_rate']:.1%}")
            Cache hit rate: 85.3%
        """
        if not self._embedding_cache:
            return {
                "size": 0,
                "max_size": 0,
                "hits": 0,
                "misses": 0,
                "hit_rate": 0.0,
            }
        return self._embedding_cache.get_stats()

    # Synchronous wrappers (for BaseStore compatibility)

    def _run_async(self, coro):
        """
        Run async coroutine, handling existing event loop gracefully.

        If called from within an async context, raises RuntimeError with helpful message.
        Otherwise creates new event loop to run the coroutine.
        """
        try:
            # Check if there's a running event loop
            loop = asyncio.get_running_loop()
            # If we get here, we're already in an async context
            raise RuntimeError(
                "Cannot use synchronous methods from within an async context. "
                "Use async methods (aget, aput, adelete, asearch, etc.) instead."
            )
        except RuntimeError as e:
            # Check if this is the "no running loop" error (expected) vs our custom error (re-raise)
            if "Cannot use synchronous methods" in str(e):
                raise
            # No running loop - safe to use asyncio.run()
            return asyncio.run(coro)

    def get(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        refresh_ttl: bool | None = None,
    ) -> Item | None:
        """Synchronous wrapper for aget()."""
        return self._run_async(
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
        return self._run_async(
            self.aput(namespace, key, value, index, ttl=ttl)
        )

    def delete(
        self,
        namespace: tuple[str, ...],
        key: str,
    ) -> None:
        """Synchronous wrapper for adelete()."""
        return self._run_async(
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
        return self._run_async(
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
        return self._run_async(
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
        return self._run_async(
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
            self.metrics.record_query(statement_name, latency_ms, success=True)

            return list(result) if result else []

        except NoHostAvailable as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_query(statement_name, latency_ms, success=False, error_type="NoHostAvailable")

            # Trigger critical alert for cluster unavailability
            if self.alert_manager:
                self.alert_manager.trigger_alert(
                    severity=AlertSeverity.CRITICAL,
                    message="ScyllaDB cluster unreachable: No hosts available",
                    context={"error": str(e), "latency_ms": latency_ms}
                )

            raise StoreConnectionError(
                "No hosts available for query execution",
                original_error=e
            )

        except (ReadTimeout, WriteTimeout) as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            operation = "read" if isinstance(e, ReadTimeout) else "write"
            self.metrics.record_query(statement_name, latency_ms, success=False, error_type=f"{operation}Timeout")
            raise StoreTimeoutError(
                f"{operation.capitalize()} operation timed out",
                original_error=e,
                operation_type=operation
            )

        except OperationTimedOut as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_query(statement_name, latency_ms, success=False, error_type="OperationTimedOut")
            raise StoreTimeoutError(
                "Client-side operation timeout",
                original_error=e
            )

        except Unavailable as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_query(statement_name, latency_ms, success=False, error_type="Unavailable")

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

            self.metrics.record_query(statement_name, latency_ms, success=False, error_type=f"{operation}Failure")

            raise StoreQueryError(
                f"Coordination failure during {operation} operation",
                original_error=e
            )

        except (Unauthorized, AuthenticationFailed) as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_query(statement_name, latency_ms, success=False, error_type="AuthError")
            raise StoreAuthenticationError(
                "Authentication or authorization failed",
                original_error=e
            )

        except (InvalidRequest, ConfigurationException) as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_query(statement_name, latency_ms, success=False, error_type="ValidationError")
            raise StoreValidationError(
                f"Invalid query or configuration: {e}",
                original_error=e
            )

        except AlreadyExists as e:
            # This is actually not an error for our use case (using IF NOT EXISTS)
            # But handle it gracefully
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_query(statement_name, latency_ms, success=True)
            logger.debug(f"AlreadyExists exception (expected): {e}")
            return []

        except RequestExecutionException as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_query(statement_name, latency_ms, success=False, error_type="RequestExecution")
            raise StoreQueryError(
                f"Query execution failed: {e}",
                original_error=e
            )

        except DriverException as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_query(statement_name, latency_ms, success=False, error_type="DriverError")
            raise ScyllaDBStoreError(
                f"Driver error: {e}",
                original_error=e
            )

        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            self.metrics.record_query(statement_name, latency_ms, success=False, error_type="UnexpectedError")
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
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
            "put_with_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                USING TTL ?
            """,
            "delete": "DELETE FROM store WHERE prefix = ? AND key = ?",
            "refresh_ttl": """
                UPDATE store USING TTL ?
                SET value = ?, updated_at = ?
                WHERE prefix = ? AND key = ?
            """,
        }

        # Note: Embeddings are ALWAYS stored in Qdrant only (never in ScyllaDB)
        # No embedding column statements needed

        # Continue with batch and LWT operations
        statements.update({
            # Batch operations
            "batch_get": "SELECT * FROM store WHERE prefix = ? AND key = ?",
            "batch_put_no_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
            "batch_put_with_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                USING TTL ?
            """,

            # Lightweight Transaction (LWT) operations
            "put_if_not_exists_no_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                IF NOT EXISTS
            """,
            "put_if_not_exists_with_ttl": """
                INSERT INTO store (prefix, key, value, created_at, updated_at, ttl_seconds)
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
        })

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
            query: CQL query string, SimpleStatement, or PreparedStatement object
            parameters: Query parameters (only used with string queries or prepared statements)
        """
        from cassandra.query import PreparedStatement

        # Handle PreparedStatement objects directly
        if isinstance(query, PreparedStatement):
            response_future = self.session.execute_async(query, parameters)
        # Handle SimpleStatement objects directly (they may have fetch_size set)
        elif isinstance(query, SimpleStatement):
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
        """
        Convert namespace tuple to prefix string using URL encoding.

        This prevents collisions like ("a.b", "c") vs ("a", "b.c") by encoding
        dots and other special characters in namespace labels.
        """
        import urllib.parse
        # URL-encode each label to escape dots and special chars, then join with unencoded dots
        encoded_labels = [urllib.parse.quote(label, safe='') for label in namespace]
        return ".".join(encoded_labels)

    def _prefix_to_namespace(self, prefix: str) -> tuple[str, ...]:
        """
        Convert prefix string back to namespace tuple using URL decoding.

        Reverses the encoding done in _namespace_to_prefix.
        """
        import urllib.parse
        # Split on dots, then URL-decode each label
        encoded_labels = prefix.split(".")
        return tuple(urllib.parse.unquote(label) for label in encoded_labels)

    def _validate_keyspace(self, keyspace: str) -> None:
        """
        Validate keyspace name to prevent CQL injection.

        Args:
            keyspace: Keyspace name

        Raises:
            StoreValidationError: If keyspace name is invalid
        """
        if not keyspace:
            raise StoreValidationError(
                "Keyspace name cannot be empty",
                field="keyspace",
                value=keyspace
            )

        if not isinstance(keyspace, str):
            raise StoreValidationError(
                f"Keyspace must be a string, got {type(keyspace).__name__}",
                field="keyspace",
                value=keyspace
            )

        # CQL identifier rules: alphanumeric and underscore only, must start with letter
        import re
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', keyspace):
            raise StoreValidationError(
                "Keyspace name must start with a letter and contain only alphanumeric characters and underscores",
                field="keyspace",
                value=keyspace
            )

        # Check length (CQL limit is 48 characters for identifiers)
        if len(keyspace) > 48:
            raise StoreValidationError(
                f"Keyspace name exceeds maximum length (48 characters): {len(keyspace)}",
                field="keyspace",
                value=keyspace
            )

        # Prevent reserved CQL keywords
        cql_keywords = {
            'select', 'insert', 'update', 'delete', 'drop', 'create', 'alter',
            'truncate', 'use', 'grant', 'revoke', 'from', 'where', 'and', 'or',
            'table', 'keyspace', 'index', 'materialized', 'view', 'type'
        }
        if keyspace.lower() in cql_keywords:
            raise StoreValidationError(
                f"Keyspace name '{keyspace}' is a reserved CQL keyword",
                field="keyspace",
                value=keyspace
            )

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

            # Note: Dots are now allowed in labels since we use URL encoding
            # to prevent collisions. The validation for dots has been removed.

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

    def _calculate_relevance_score(
        self,
        value: dict[str, Any],
        filter: dict[str, Any] | None,
        created_at: datetime,
        updated_at: datetime,
        query: str | None
    ) -> float:
        """
        Calculate relevance score for search results.

        Scoring formula combines multiple signals:
        - Recency: More recently updated items score higher (0.0-0.4)
        - Text relevance: Query matches in value boost score (0.0-0.3)
        - Filter quality: Exact filter matches boost score (0.0-0.3)
        - Base score: All items start with 0.0

        Returns:
            Float between 0.0 and 1.0 (higher is more relevant)
        """
        score = 0.0

        # Component 1: Recency score (40% weight)
        # Items updated within last 24h get bonus, decaying over 30 days
        now = datetime.now(timezone.utc)

        # Make updated_at timezone-aware if it's naive
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        age_seconds = (now - updated_at).total_seconds()

        if age_seconds < 86400:  # < 24 hours
            recency_score = 0.4
        elif age_seconds < 604800:  # < 7 days
            recency_score = 0.3
        elif age_seconds < 2592000:  # < 30 days
            recency_score = 0.2
        else:
            recency_score = 0.1

        score += recency_score

        # Component 2: Text relevance (30% weight)
        if query:
            query_lower = query.lower()
            value_text = json.dumps(value).lower()

            # Simple text matching - boost score if query terms found
            query_terms = query_lower.split()
            matches = sum(1 for term in query_terms if term in value_text)

            if matches > 0:
                # Normalize by number of query terms
                text_score = min(0.3, (matches / len(query_terms)) * 0.3)
                score += text_score

        # Component 3: Filter match quality (30% weight)
        if filter:
            # Count how many filter conditions are satisfied
            total_conditions = len(filter)
            matched_conditions = 0

            for key, condition in filter.items():
                field_value = self._get_nested_value(value, key)

                if isinstance(condition, dict):
                    # Operator-based filter - check if it passes
                    for op, target in condition.items():
                        if op == "$eq" and field_value == target:
                            matched_conditions += 1
                        elif op == "$ne" and field_value != target:
                            matched_conditions += 1
                        elif op in ("$gt", "$gte", "$lt", "$lte"):
                            # Range queries get partial credit
                            matched_conditions += 0.5
                else:
                    # Simple equality - full credit
                    if field_value == condition:
                        matched_conditions += 1

            if total_conditions > 0:
                filter_score = (matched_conditions / total_conditions) * 0.3
                score += filter_score

        # Ensure score is within [0.0, 1.0]
        return min(1.0, max(0.0, score))

    async def _refresh_ttl(self, prefix: str, key: str, value: str, ttl_seconds: int) -> None:
        """Refresh TTL for an item (ttl_seconds is already in seconds)."""
        now = datetime.now(timezone.utc)
        await self._execute_prepared("refresh_ttl", (ttl_seconds, value, now, prefix, key))

    async def _batch_refresh_ttls(
        self,
        items: list[tuple[str, str, str, int]]  # [(prefix, key, value, ttl_seconds), ...]
    ) -> None:
        """
        Batch refresh TTLs using concurrent execution.

        Significantly improves performance by executing multiple TTL refreshes in parallel
        instead of sequentially. Critical for search operations that return many results.
        """
        if not items:
            return

        prepared = self._prepared_statements.get("refresh_ttl")
        if not prepared:
            logger.warning("refresh_ttl statement not prepared, skipping batch refresh")
            return

        now = datetime.now(timezone.utc)
        params_list = [(ttl, value, now, prefix, key) for prefix, key, value, ttl in items]

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: list(execute_concurrent_with_args(
                self.session, prepared, params_list, concurrency=100
            ))
        )
        logger.debug(f"Batch refreshed {len(items)} TTLs")

    async def _batch_fetch_items(
        self,
        keys_to_fetch: list[tuple[tuple[str, ...], str]]  # [(namespace, key), ...]
    ) -> list[Item | None]:
        """
        Batch fetch items using concurrent execution.

        Fixes N+1 query pattern in Qdrant search. Instead of making N individual
        queries, executes them concurrently for ~10x performance improvement.
        """
        if not keys_to_fetch:
            return []

        prepared = self._prepared_statements.get("batch_get")
        if not prepared:
            logger.warning("batch_get statement not prepared, falling back to sequential fetch")
            # Fallback to sequential (slower but works)
            items = []
            for ns, key in keys_to_fetch:
                item = await self.aget(ns, key)
                items.append(item)
            return items

        params_list = [
            (self._namespace_to_prefix(ns), key)
            for ns, key in keys_to_fetch
        ]

        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None,
            lambda: list(execute_concurrent_with_args(
                self.session, prepared, params_list, concurrency=100
            ))
        )

        items = []
        for (ns, key), (success, result) in zip(keys_to_fetch, results):
            if success and result:
                row = result.one() if hasattr(result, 'one') else (result[0] if result else None)
                if not row:
                    continue
                items.append(Item(
                    value=json.loads(row.value),
                    key=row.key,
                    namespace=ns,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                ))
            else:
                items.append(None)

        logger.debug(f"Batch fetched {len(items)} items")
        return items

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
                row = result.one() if hasattr(result, 'one') else (result[0] if result else None)
                if not row:
                    results[idx] = None
                    continue

                should_refresh = op.refresh_ttl if op.refresh_ttl is not None else self.ttl_config.get("refresh_on_read", True)

                # Refresh TTL if needed
                if should_refresh and row.ttl_seconds:
                    prefix = self._namespace_to_prefix(op.namespace)
                    await self._refresh_ttl(prefix, op.key, row.ttl_seconds)

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

            # Determine TTL (in seconds)
            ttl_seconds = None

            if op.ttl is not NOT_PROVIDED and op.ttl is not None:
                ttl_seconds = int(op.ttl)
            elif self.ttl_config.get("default_ttl"):
                ttl_seconds = int(self.ttl_config["default_ttl"])

            # Add to batch with appropriate prepared statement
            if ttl_seconds:
                prepared = self._prepared_statements.get("batch_put_with_ttl")
                batch.add(prepared, (prefix, op.key, value_json, now, now, ttl_seconds, ttl_seconds))
            else:
                prepared = self._prepared_statements.get("batch_put_no_ttl")
                batch.add(prepared, (prefix, op.key, value_json, now, now, None))

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

            # Batch sync to Qdrant if indexing configured
            if self.index_config:
                # Generate embeddings in batch (single API call - much faster!)
                values = [op.value for op in put_ops]
                embeddings = await self._generate_embeddings_batch(values)

                # Prepare Qdrant items
                qdrant_items = []
                import hashlib
                for op, embedding in zip(put_ops, embeddings):
                    if embedding:
                        prefix = self._namespace_to_prefix(op.namespace)
                        point_id = hashlib.md5(f"{prefix}.{op.key}".encode()).hexdigest()
                        qdrant_items.append((point_id, embedding, op.namespace, op.key, op.value))

                if qdrant_items:
                    # Fire-and-forget Qdrant sync for speed
                    asyncio.create_task(self._batch_sync_to_qdrant(qdrant_items))

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
            ttl_seconds = None
            if op.ttl is not NOT_PROVIDED and op.ttl is not None:
                ttl_seconds = int(op.ttl)
            elif self.ttl_config.get("default_ttl"):
                ttl_seconds = int(self.ttl_config["default_ttl"])

            if ttl_seconds:
                ops_with_ttl.append((idx, op, ttl_seconds))
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
            for idx, op, ttl_seconds in ops_with_ttl:
                prefix = self._namespace_to_prefix(op.namespace)
                value_json = json.dumps(op.value)
                params_list.append((prefix, op.key, value_json, now, now, ttl_seconds, ttl_seconds))

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: list(execute_concurrent_with_args(
                    self.session, prepared, params_list, concurrency=50
                ))
            )

            for idx, _, _ in ops_with_ttl:
                results[idx] = None

        # Batch generate embeddings and sync to Qdrant if indexing configured
        if self.index_config:
            # Extract all operations (both with and without TTL)
            all_ops = [op for _, op in ops_without_ttl] + [op for _, op, _ in ops_with_ttl]

            # Generate embeddings in batch (single API call - much faster!)
            values = [op.value for op in all_ops]
            embeddings = await self._generate_embeddings_batch(values)

            # Prepare Qdrant items
            qdrant_items = []
            import hashlib
            for op, embedding in zip(all_ops, embeddings):
                if embedding:
                    prefix = self._namespace_to_prefix(op.namespace)
                    point_id = hashlib.md5(f"{prefix}.{op.key}".encode()).hexdigest()
                    qdrant_items.append((point_id, embedding, op.namespace, op.key, op.value))

            if qdrant_items:
                # Fire-and-forget Qdrant sync for speed
                asyncio.create_task(self._batch_sync_to_qdrant(qdrant_items))

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

    async def aclose(self) -> None:
        """
        Gracefully shutdown the store and cleanup resources.

        This method:
        - Stops TTL sweeper task
        - Closes Qdrant client connection
        - Clears prepared statement cache
        - Clears embedding cache
        - Does NOT close the ScyllaDB session (caller's responsibility)

        Example:
            async with AsyncScyllaDBStore.from_contact_points(...) as store:
                # ... use store
                pass  # aclose() called automatically

            # Or manually:
            store = AsyncScyllaDBStore(...)
            try:
                await store.setup()
                # ... use store
            finally:
                await store.aclose()
        """
        logger.info(f"Shutting down AsyncScyllaDBStore for keyspace '{self.keyspace}'...")

        # Stop TTL sweeper task
        if self._ttl_sweeper_task and not self._ttl_sweeper_task.done():
            logger.info("Stopping TTL sweeper task...")
            self._ttl_stop_event.set()
            try:
                await asyncio.wait_for(self._ttl_sweeper_task, timeout=5.0)
                logger.info("TTL sweeper task stopped successfully")
            except asyncio.TimeoutError:
                logger.warning("TTL sweeper task did not stop within timeout, cancelling...")
                self._ttl_sweeper_task.cancel()
                try:
                    await self._ttl_sweeper_task
                except asyncio.CancelledError:
                    pass

        # Close Qdrant client
        if hasattr(self, 'qdrant_client') and self.qdrant_client:
            logger.info("Closing Qdrant client...")
            try:
                await asyncio.wait_for(self.qdrant_client.close(), timeout=5.0)
                logger.info("Qdrant client closed successfully")
            except asyncio.TimeoutError:
                logger.warning("Qdrant client close timed out")
            except Exception as e:
                logger.warning(f"Error closing Qdrant client: {e}")

        # Clear caches
        logger.info("Clearing caches...")
        self._prepared_statements.clear()
        self._embedding_cache.clear()

        # Export final metrics if tracer is enabled
        if self.tracer:
            logger.info("Exporting final traces...")
            # OpenTelemetry auto-exports on shutdown

        logger.info(f"AsyncScyllaDBStore shutdown complete for keyspace '{self.keyspace}'")

    async def health_check(self) -> dict[str, Any]:
        """
        Perform health check on all connections.

        Returns:
            Dictionary with health status for ScyllaDB and Qdrant

        Example:
            health = await store.health_check()
            print(health)
            # {
            #     "scylladb": {"status": "healthy", "latency_ms": 2.5},
            #     "qdrant": {"status": "healthy", "latency_ms": 1.2},
            #     "overall": "healthy"
            # }
        """
        health_status = {}

        # Check ScyllaDB
        try:
            start = time.perf_counter()
            await self._execute_async("SELECT now() FROM system.local")
            latency_ms = (time.perf_counter() - start) * 1000
            health_status["scylladb"] = {
                "status": "healthy",
                "latency_ms": round(latency_ms, 2)
            }
        except Exception as e:
            health_status["scylladb"] = {
                "status": "unhealthy",
                "error": str(e)
            }

        # Check Qdrant
        if hasattr(self, 'qdrant_client') and self.qdrant_client:
            try:
                start = time.perf_counter()
                await self.qdrant_client.get_collections()
                latency_ms = (time.perf_counter() - start) * 1000
                health_status["qdrant"] = {
                    "status": "healthy",
                    "latency_ms": round(latency_ms, 2)
                }
            except Exception as e:
                health_status["qdrant"] = {
                    "status": "unhealthy",
                    "error": str(e)
                }

        # Overall status
        all_healthy = all(
            component.get("status") == "healthy"
            for component in health_status.values()
        )
        health_status["overall"] = "healthy" if all_healthy else "unhealthy"

        return health_status
