"""
Vertector ScyllaDB Store - Production-ready ScyllaDB store with vector search.

This package provides a high-performance, production-ready implementation
of a ScyllaDB-backed store with vector search capabilities via Qdrant.
"""

from vertector_scylladbstore.store import (
    AsyncScyllaDBStore,
    Item,
    PutOp,
    GetOp,
    SearchOp,
    ListNamespacesOp,
    ScyllaDBStoreError,
    StoreConnectionError,
    StoreQueryError,
    StoreValidationError,
    StoreConfigurationError,
    StoreTimeoutError,
    StoreUnavailableError,
    StoreAuthenticationError,
    CircuitBreaker,
    TTLConfig,
    IndexConfig,
    ScyllaIndexConfig,
    ExecutionProfileConfig,
)

from vertector_scylladbstore.config import (
    ScyllaDBStoreConfig,
    AuthConfig,
    TLSConfig,
    RetryConfig,
    CircuitBreakerConfig,
    PoolConfig,
    MetricsConfig,
    RateLimitConfig,
    QdrantConfig,
    SecretsManager,
    SecretsProvider,
    load_config_from_env,
)

from vertector_scylladbstore.observability import (
    Tracer,
    EnhancedMetrics,
    AlertManager,
    AlertSeverity,
)

from vertector_scylladbstore.rate_limiter import (
    TokenBucketRateLimiter,
    SlidingWindowRateLimiter,
    RateLimitExceeded,
    rate_limit,
)

__version__ = "1.0.0"

__all__ = [
    # Core store
    "AsyncScyllaDBStore",
    "Item",
    "PutOp",
    "GetOp",
    "SearchOp",
    "ListNamespacesOp",
    "ScyllaDBStoreError",
    "StoreConnectionError",
    "StoreQueryError",
    "StoreValidationError",
    "StoreConfigurationError",
    "StoreTimeoutError",
    "StoreUnavailableError",
    "StoreAuthenticationError",
    "CircuitBreaker",
    "TTLConfig",
    "IndexConfig",
    "ScyllaIndexConfig",
    "ExecutionProfileConfig",
    # Configuration
    "ScyllaDBStoreConfig",
    "AuthConfig",
    "TLSConfig",
    "RetryConfig",
    "CircuitBreakerConfig",
    "PoolConfig",
    "MetricsConfig",
    "RateLimitConfig",
    "QdrantConfig",
    "SecretsManager",
    "SecretsProvider",
    "load_config_from_env",
    # Observability
    "Tracer",
    "EnhancedMetrics",
    "AlertManager",
    "AlertSeverity",
    # Rate limiting
    "TokenBucketRateLimiter",
    "SlidingWindowRateLimiter",
    "RateLimitExceeded",
    "rate_limit",
]
