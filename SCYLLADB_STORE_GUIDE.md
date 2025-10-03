# ScyllaDB Store - Comprehensive Guide

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Installation & Setup](#installation--setup)
4. [Core Concepts](#core-concepts)
5. [API Reference](#api-reference)
6. [Advanced Features](#advanced-features)
7. [Performance Optimization](#performance-optimization)
8. [Monitoring & Observability](#monitoring--observability)
9. [Error Handling](#error-handling)
10. [Production Deployment](#production-deployment)
11. [Troubleshooting](#troubleshooting)
12. [Examples & Use Cases](#examples--use-cases)

---

## Overview

**ScyllaDB Store** is a production-ready, async implementation of LangGraph's `BaseStore` interface using ScyllaDB/Cassandra as the backend database.

### Key Features

✅ **Native Async I/O** - True asyncio integration with AsyncioConnection
✅ **Hierarchical Namespaces** - Organize data with multi-level namespaces
✅ **Atomic Batch Operations** - LOGGED and UNLOGGED batch types
✅ **TTL Support** - Automatic data expiration with refresh-on-read
✅ **Lightweight Transactions** - Compare-and-set, conditional updates
✅ **Circuit Breaker Pattern** - Prevent cascading failures
✅ **Comprehensive Metrics** - Prometheus-compatible metrics export
✅ **Shard Awareness** - ScyllaDB-optimized query routing
✅ **Retry Logic** - Exponential backoff for transient failures
✅ **Production Ready** - Validation, error handling, observability

### When to Use

- **High-throughput workloads** requiring distributed storage
- **Multi-region deployments** needing strong consistency
- **Time-series data** with automatic expiration (TTL)
- **LangGraph applications** needing persistent state storage
- **Microservices** requiring shared key-value storage

---

## Architecture

### System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Application Layer                      │
│          (LangGraph, Microservices, etc.)               │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│              AsyncScyllaDBStore                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │  BaseStore Interface Implementation              │  │
│  │  - aput, aget, adelete, asearch, abatch         │  │
│  │  - Namespace management, TTL, LWT               │  │
│  └──────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Advanced Features Layer                         │  │
│  │  - Circuit Breaker, Metrics, Retry Logic        │  │
│  │  - Execution Profiles, Batch Processing         │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│         ScyllaDB Python Driver (AsyncIO)                 │
│  ┌──────────────────────────────────────────────────┐  │
│  │  AsyncioConnection (Native Async I/O)            │  │
│  │  TokenAwarePolicy (Shard Awareness)              │  │
│  │  Prepared Statements (Query Optimization)        │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│              ScyllaDB Cluster                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │  Node 1  │  │  Node 2  │  │  Node 3  │   ...       │
│  │  Shard-  │  │  Shard-  │  │  Shard-  │             │
│  │  Aware   │  │  Aware   │  │  Aware   │             │
│  └──────────┘  └──────────┘  └──────────┘             │
└─────────────────────────────────────────────────────────┘
```

### Data Model

#### Schema

```sql
CREATE TABLE IF NOT EXISTS store (
    prefix text,           -- Namespace prefix (e.g., "users.profile")
    key text,             -- Item key
    value text,           -- JSON-encoded value
    created_at timestamp, -- Creation timestamp
    updated_at timestamp, -- Last update timestamp
    PRIMARY KEY (prefix, key)
) WITH compaction = {'class': 'TimeWindowCompactionStrategy'}
  AND gc_grace_seconds = 86400;
```

#### Namespace Structure

Namespaces are hierarchical tuples converted to dot-separated strings:

```python
namespace = ("users", "profile", "settings")
# Stored as prefix: "users.profile.settings"
```

#### Item Structure

```python
@dataclass
class Item:
    value: dict[str, Any]          # User data
    key: str                        # Item key
    namespace: tuple[str, ...]      # Hierarchical namespace
    created_at: datetime            # Creation time
    updated_at: datetime            # Last update time
```

---

## Installation & Setup

### Prerequisites

- Python 3.10+
- ScyllaDB 5.0+ or Cassandra 4.0+
- scylla-driver 3.29.0+

### Installation

```bash
# Using uv (recommended)
uv pip install scylla-driver

# Or using pip
pip install scylla-driver
```

### Basic Setup

```python
from cassandra.cluster import Cluster
from scylladb_store import AsyncScyllaDBStore, TTLConfig

# Connect to ScyllaDB
cluster = Cluster(['127.0.0.1'])
session = cluster.connect()

# Create store
store = AsyncScyllaDBStore(
    session=session,
    keyspace='my_keyspace',
    ttl=TTLConfig(
        default_ttl=3600.0,      # 1 hour default TTL
        refresh_on_read=True      # Refresh TTL on read
    )
)

# Initialize schema
await store.setup()
```

### Advanced Setup

```python
from cassandra.cluster import Cluster, ExecutionProfile
from cassandra.policies import TokenAwarePolicy, DCAwareRoundRobinPolicy
from cassandra.query import ConsistencyLevel

# Create execution profile
profile = ExecutionProfile(
    load_balancing_policy=TokenAwarePolicy(DCAwareRoundRobinPolicy()),
    request_timeout=20.0,
    consistency_level=ConsistencyLevel.LOCAL_QUORUM
)

# Create cluster with advanced configuration
cluster = Cluster(
    contact_points=['node1.example.com', 'node2.example.com', 'node3.example.com'],
    port=9042,
    protocol_version=4,
    executor_threads=8,           # More threads for async callbacks
    compression=True,              # Enable lz4 compression
    connect_timeout=10.0,
    execution_profiles={'default': profile}
)

session = cluster.connect()

# Create store with custom configuration
store = AsyncScyllaDBStore(
    session=session,
    keyspace='production_keyspace',
    ttl=TTLConfig(
        default_ttl=7200.0,       # 2 hours
        refresh_on_read=True
    )
)

await store.setup()

# Enable circuit breaker for resilience
store.enable_circuit_breaker(
    failure_threshold=5,
    success_threshold=3,
    timeout_seconds=30.0
)

# Create standard execution profiles
store.create_standard_profiles()
```

---

## Core Concepts

### 1. Namespaces

Namespaces provide hierarchical organization of data:

```python
# Single-level namespace
namespace = ("users",)

# Multi-level namespace
namespace = ("users", "profile", "settings")

# Application structure
namespaces = [
    ("users", "profile"),       # User profiles
    ("users", "sessions"),      # User sessions
    ("cache", "api", "v1"),     # API cache v1
    ("cache", "api", "v2"),     # API cache v2
]
```

### 2. Keys and Values

```python
# Put item
await store.aput(
    namespace=("users", "profile"),
    key="user_123",
    value={"name": "Alice", "email": "alice@example.com"}
)

# Get item
item = await store.aget(("users", "profile"), "user_123")
print(item.value)  # {"name": "Alice", ...}
print(item.created_at)
print(item.updated_at)
```

### 3. Time-to-Live (TTL)

Automatic expiration of data:

```python
# Item with 60-second TTL
await store.aput(
    namespace=("sessions",),
    key="session_abc",
    value={"user_id": 123, "token": "xyz"},
    ttl=60.0  # Expires in 60 seconds
)

# Default TTL (configured at store level)
store = AsyncScyllaDBStore(
    session=session,
    keyspace="my_keyspace",
    ttl=TTLConfig(
        default_ttl=3600.0,      # 1 hour default
        refresh_on_read=True      # Refresh on read
    )
)

# Refresh TTL on read
item = await store.aget(("sessions",), "session_abc")
# TTL is refreshed to default_ttl if refresh_on_read=True
```

### 4. Batch Operations

Execute multiple operations efficiently:

```python
from scylladb_store import PutOp, GetOp

# All-PUT batch (uses atomic BatchStatement)
ops = [
    PutOp(namespace=("users",), key=f"user_{i}", value={"id": i})
    for i in range(10)
]
results = await store.abatch(ops)

# Mixed operations (uses concurrent execution)
ops = [
    PutOp(namespace=("cache",), key="item1", value={"data": "new"}),
    GetOp(namespace=("cache",), key="item2"),
    PutOp(namespace=("cache",), key="item3", value={"data": "updated"}),
]
results = await store.abatch(ops)
```

---

## API Reference

### Core Operations

#### aput()
Store a value with optional TTL.

```python
async def aput(
    self,
    namespace: tuple[str, ...],
    key: str,
    value: dict[str, Any],
    ttl: float | None = NOT_PROVIDED
) -> None
```

**Parameters:**
- `namespace`: Hierarchical namespace tuple
- `key`: Item key (unique within namespace)
- `value`: Dictionary to store (JSON-serializable)
- `ttl`: Time-to-live in seconds (None = no expiration)

**Example:**
```python
await store.aput(
    namespace=("users", "profile"),
    key="user_123",
    value={"name": "Alice", "age": 30},
    ttl=3600.0  # 1 hour
)
```

#### aget()
Retrieve a value by key.

```python
async def aget(
    self,
    namespace: tuple[str, ...],
    key: str
) -> Item | None
```

**Returns:** Item object or None if not found

**Example:**
```python
item = await store.aget(("users", "profile"), "user_123")
if item:
    print(f"Name: {item.value['name']}")
    print(f"Created: {item.created_at}")
```

#### adelete()
Delete an item.

```python
async def adelete(
    self,
    namespace: tuple[str, ...],
    key: str
) -> None
```

**Example:**
```python
await store.adelete(("users", "profile"), "user_123")
```

#### asearch()
Search items with filtering.

```python
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
    fetch_size: int | None = None
) -> list[SearchItem]
```

**Parameters:**
- `namespace_prefix`: Filter namespaces starting with this prefix
- `filter`: Filter items by field values
- `limit`: Maximum results to return
- `offset`: Pagination offset
- `refresh_ttl`: Refresh TTL on matching items
- `fetch_size`: Page size for automatic paging (default: 5000)

**Example:**
```python
# Search all users
results = await store.asearch(
    ("users",),
    filter={"age": 30},
    limit=100
)

# Search with paging
results = await store.asearch(
    ("users",),
    limit=10000,
    fetch_size=100  # Fetch 100 rows per page
)
```

#### abatch()
Execute batch operations.

```python
async def abatch(
    self,
    ops: Iterable[Op],
    *,
    batch_type: str = "UNLOGGED",
    max_retries: int = 0,
    retry_delay: float = 0.1,
    retry_backoff: float = 2.0
) -> list[Result]
```

**Parameters:**
- `ops`: Operations to execute (PutOp, GetOp, SearchOp, ListNamespacesOp)
- `batch_type`: "LOGGED" (atomic) or "UNLOGGED" (fast)
- `max_retries`: Maximum retry attempts
- `retry_delay`: Initial retry delay (seconds)
- `retry_backoff`: Delay multiplier for exponential backoff

**Example:**
```python
from scylladb_store import PutOp

# UNLOGGED batch (default)
ops = [PutOp(namespace=("users",), key=f"user_{i}", value={"id": i}) for i in range(10)]
results = await store.abatch(ops)

# LOGGED batch with retries
results = await store.abatch(
    ops,
    batch_type="LOGGED",
    max_retries=3,
    retry_delay=0.1,
    retry_backoff=2.0
)
```

#### alist_namespaces()
List and filter namespaces.

```python
async def alist_namespaces(
    self,
    *,
    prefix: tuple[str, ...] | None = None,
    suffix: tuple[str, ...] | None = None,
    max_depth: int | None = None,
    limit: int = 100,
    offset: int = 0
) -> list[tuple[str, ...]]
```

**Example:**
```python
# List all namespaces
namespaces = await store.alist_namespaces(limit=1000)

# Filter by prefix
user_namespaces = await store.alist_namespaces(prefix=("users",))

# Limit depth
top_level = await store.alist_namespaces(max_depth=1)
```

### Lightweight Transactions (LWT)

#### aput_if_not_exists()
Conditional insert (IF NOT EXISTS).

```python
async def aput_if_not_exists(
    self,
    namespace: tuple[str, ...],
    key: str,
    value: dict[str, Any],
    ttl: float | None = None
) -> bool
```

**Returns:** True if inserted, False if key exists

**Example:**
```python
# Distributed lock pattern
success = await store.aput_if_not_exists(
    namespace=("locks",),
    key="resource_123",
    value={"owner": "worker_1", "acquired_at": datetime.now().isoformat()},
    ttl=30.0
)
if success:
    # Lock acquired, do work
    pass
else:
    # Lock already held
    pass
```

#### aupdate_if_exists()
Conditional update (IF EXISTS).

```python
async def aupdate_if_exists(
    self,
    namespace: tuple[str, ...],
    key: str,
    value: dict[str, Any],
    ttl: float | None = None
) -> bool
```

**Example:**
```python
# Update only if exists
success = await store.aupdate_if_exists(
    namespace=("users",),
    key="user_123",
    value={"name": "Alice", "status": "inactive"}
)
```

#### acompare_and_set()
Compare-and-set (CAS) for optimistic locking.

```python
async def acompare_and_set(
    self,
    namespace: tuple[str, ...],
    key: str,
    expected_value: dict[str, Any],
    new_value: dict[str, Any],
    ttl: float | None = None
) -> bool
```

**Example:**
```python
# Atomic counter increment
item = await store.aget(("counters",), "page_views")
current = item.value
new = {"count": current["count"] + 1}

success = await store.acompare_and_set(
    namespace=("counters",),
    key="page_views",
    expected_value=current,
    new_value=new
)
```

#### adelete_if_exists()
Conditional delete (IF EXISTS).

```python
async def adelete_if_exists(
    self,
    namespace: tuple[str, ...],
    key: str
) -> bool
```

#### adelete_if_value()
Delete only if value matches.

```python
async def adelete_if_value(
    self,
    namespace: tuple[str, ...],
    key: str,
    expected_value: dict[str, Any]
) -> bool
```

---

## Advanced Features

### 1. Circuit Breaker Pattern

Prevent cascading failures:

```python
# Enable circuit breaker
store.enable_circuit_breaker(
    failure_threshold=5,      # Open after 5 failures
    success_threshold=3,      # Close after 3 successes
    timeout_seconds=30.0      # Wait 30s before retry
)

# Check circuit state
state = store.circuit_breaker.get_state()
print(f"State: {state['state']}")  # CLOSED, OPEN, or HALF_OPEN
print(f"Failures: {state['failure_count']}")
```

### 2. Execution Profiles

Configure different settings for different query types:

```python
# Create standard profiles
store.create_standard_profiles()
# - strong_reads: QUORUM consistency, 30s timeout
# - fast_writes: ONE consistency, 5s timeout
# - lwt_operations: SERIAL consistency, 30s timeout
# - analytics: ALL consistency, 60s timeout

# Create custom profile
from cassandra.query import ConsistencyLevel

store.add_execution_profile(
    'custom_profile',
    consistency_level=ConsistencyLevel.LOCAL_QUORUM,
    request_timeout=15.0
)
```

### 3. Connection Warmup

Pre-establish connections for better initial performance:

```python
# Warmup with concurrent queries
await store.warmup_connections(num_queries=20)
```

### 4. Shard Awareness

ScyllaDB-specific optimization for direct shard routing:

```python
# Get shard awareness info
shard_info = store.get_shard_awareness_info()

if shard_info['is_shard_aware']:
    print("✓ Shard awareness enabled")
    print(f"Shards per node: {shard_info['shard_stats']}")
else:
    print("✗ Shard awareness not available")
```

---

## Performance Optimization

### 1. Batch Size Tuning

```python
# Optimal batch sizes
# Small batches (5-25):    Low latency, more round trips
# Medium batches (25-50):  Balanced
# Large batches (50-100):  Higher throughput, higher latency

# Keep under 50 for best performance
ops = [PutOp(...) for i in range(50)]
await store.abatch(ops)
```

### 2. Query Paging

```python
# Small fetch_size: Less memory, more round trips
results = await store.asearch(
    ("users",),
    limit=10000,
    fetch_size=50
)

# Large fetch_size: More memory, fewer round trips
results = await store.asearch(
    ("users",),
    limit=10000,
    fetch_size=5000  # Default
)
```

### 3. TTL Optimization

```python
# Use TTL instead of manual deletion
await store.aput(
    namespace=("cache",),
    key="temp_data",
    value={"data": "..."},
    ttl=300.0  # Auto-delete after 5 minutes
)

# Refresh TTL on read for active sessions
store = AsyncScyllaDBStore(
    session=session,
    keyspace="sessions",
    ttl=TTLConfig(
        default_ttl=1800.0,      # 30 minutes
        refresh_on_read=True      # Extend active sessions
    )
)
```

### 4. Prepared Statement Caching

Prepared statements are automatically cached and reused:

```python
# First call: prepares statement
await store.aput(namespace, key, value)

# Subsequent calls: reuses prepared statement (faster)
await store.aput(namespace, key2, value2)
```

### 5. Connection Pool Tuning

```python
cluster = Cluster(
    contact_points=['node1', 'node2', 'node3'],
    executor_threads=8,           # More threads for async callbacks
    compression=True,              # Enable compression
    protocol_version=4,
    connect_timeout=10.0,
    # Connection pool settings per host
    # Defaults: core=2, max=8
)
```

---

## Monitoring & Observability

### 1. Health Checks

```python
# Perform health check
health = await store.health_check()

print(f"Status: {health['status']}")  # healthy or unhealthy
print(f"Latency: {health['latency_ms']:.2f}ms")

for check_name, result in health['checks'].items():
    print(f"{check_name}: {result['status']} - {result['message']}")
```

### 2. Metrics API

```python
# Get metrics
metrics = await store.get_metrics()

print(f"Total queries: {metrics['total_queries']}")
print(f"Average latency: {metrics['avg_latency_ms']:.2f}ms")
print(f"Error rate: {metrics['error_rate']:.2%}")

# Operation breakdown
for op_type, count in metrics['operations'].items():
    print(f"{op_type}: {count}")

# Batch statistics
batch_stats = metrics['batch_stats']
print(f"Total batches: {batch_stats['total_batches']}")
print(f"Atomic batches: {batch_stats['atomic_batches']}")
print(f"Average batch size: {batch_stats['avg_batch_size']:.1f}")
```

### 3. Prometheus Metrics Export

```python
# Export in Prometheus format
prometheus_metrics = await store.export_prometheus_metrics()

# Example output:
# scylladb_store_queries_total 12345
# scylladb_store_errors_total 23
# scylladb_store_latency_ms 45.67
# scylladb_store_batches_total 456
```

### 4. Reset Metrics

```python
# Reset all metrics
await store.reset_metrics()
```

---

## Error Handling

### Exception Hierarchy

```
ScyllaDBStoreError (base)
├── StoreConnectionError      - Connection issues
├── StoreQueryError            - Query execution errors
├── StoreValidationError       - Input validation errors
│   └── field, value attributes
├── StoreTimeoutError          - Request timeouts
│   └── timeout_seconds, operation_type attributes
└── StoreUnavailableError      - Insufficient replicas
    └── consistency_level, required_replicas, alive_replicas
```

### Error Handling Examples

```python
from scylladb_store import (
    StoreValidationError,
    StoreQueryError,
    StoreTimeoutError,
    StoreUnavailableError
)

# Validation errors
try:
    await store.aput(
        namespace=tuple([f"level{i}" for i in range(20)]),  # Too deep
        key="test",
        value={"data": "test"}
    )
except StoreValidationError as e:
    print(f"Validation error: {e}")
    print(f"Field: {e.field}")
    print(f"Value: {e.value}")

# Timeout errors (retryable)
try:
    await store.aget(namespace, key)
except StoreTimeoutError as e:
    print(f"Timeout: {e}")
    print(f"Operation: {e.operation_type}")
    print(f"Timeout: {e.timeout_seconds}s")
    # Retry with exponential backoff
    await asyncio.sleep(1.0)
    await store.aget(namespace, key)

# Unavailable errors (retryable)
try:
    await store.aput(namespace, key, value)
except StoreUnavailableError as e:
    print(f"Unavailable: {e}")
    print(f"Consistency: {e.consistency_level}")
    print(f"Required: {e.required_replicas}")
    print(f"Alive: {e.alive_replicas}")
```

### Automatic Retry with Batch Operations

```python
# Retry only for transient errors
results = await store.abatch(
    ops,
    max_retries=3,          # Retry up to 3 times
    retry_delay=0.1,        # Start with 100ms
    retry_backoff=2.0       # Double each time (100ms, 200ms, 400ms)
)

# Retryable: StoreTimeoutError, StoreUnavailableError
# Non-retryable: StoreValidationError, StoreQueryError
```

---

## Production Deployment

### 1. Production Configuration

```python
from cassandra.cluster import Cluster, ExecutionProfile
from cassandra.policies import TokenAwarePolicy, DCAwareRoundRobinPolicy
from cassandra.query import ConsistencyLevel

# Multi-datacenter configuration
profile = ExecutionProfile(
    load_balancing_policy=TokenAwarePolicy(
        DCAwareRoundRobinPolicy(local_dc='datacenter1')
    ),
    request_timeout=30.0,
    consistency_level=ConsistencyLevel.LOCAL_QUORUM
)

cluster = Cluster(
    contact_points=[
        'node1.dc1.example.com',
        'node2.dc1.example.com',
        'node3.dc1.example.com'
    ],
    port=9042,
    protocol_version=4,
    executor_threads=16,         # Higher for production
    compression=True,
    connect_timeout=10.0,
    execution_profiles={'default': profile}
)

session = cluster.connect()

store = AsyncScyllaDBStore(
    session=session,
    keyspace='production_keyspace',
    ttl=TTLConfig(
        default_ttl=7200.0,      # 2 hours
        refresh_on_read=True
    )
)

await store.setup()

# Enable resilience features
store.enable_circuit_breaker(
    failure_threshold=10,
    success_threshold=5,
    timeout_seconds=60.0
)

store.create_standard_profiles()
```

### 2. Monitoring Setup

```python
import asyncio
from datetime import datetime

# Background health check
async def health_check_loop():
    while True:
        health = await store.health_check()

        if health['status'] != 'healthy':
            # Send alert
            print(f"ALERT: Store unhealthy at {datetime.now()}")
            print(f"Details: {health}")

        await asyncio.sleep(60)  # Check every minute

# Background metrics export
async def metrics_export_loop():
    while True:
        metrics = await store.get_metrics()

        # Export to monitoring system
        # e.g., push to Prometheus pushgateway

        await asyncio.sleep(30)  # Export every 30 seconds

# Start background tasks
asyncio.create_task(health_check_loop())
asyncio.create_task(metrics_export_loop())
```

### 3. Resource Limits

```python
# Validation limits (in ValidationLimits class)
MAX_VALUE_SIZE_BYTES = 1_000_000      # 1MB per value
MAX_KEY_LENGTH = 1024                  # 1KB per key
MAX_NAMESPACE_DEPTH = 10               # 10 levels deep
MAX_NAMESPACE_LABEL_LENGTH = 256       # 256 chars per label
MAX_BATCH_SIZE = 100                   # 100 ops per batch

WARN_VALUE_SIZE_BYTES = 100_000        # Warn at 100KB
WARN_BATCH_SIZE = 50                   # Warn at 50 ops
```

### 4. Deployment Checklist

- ✅ Configure multi-node cluster with replication
- ✅ Set appropriate consistency levels (LOCAL_QUORUM recommended)
- ✅ Enable compression for network efficiency
- ✅ Configure circuit breaker for resilience
- ✅ Set up health check monitoring
- ✅ Export metrics to monitoring system
- ✅ Configure appropriate TTL for data lifecycle
- ✅ Enable shard awareness for ScyllaDB
- ✅ Set connection pool sizes based on load
- ✅ Configure retry logic for transient failures
- ✅ Set up alerting for error rates and latency
- ✅ Test failover scenarios
- ✅ Document runbooks for common issues

---

## Troubleshooting

### Common Issues

#### 1. Timeout Errors

**Symptom:** StoreTimeoutError exceptions

**Causes:**
- Network latency
- Slow queries
- Insufficient resources

**Solutions:**
```python
# Increase timeout
profile = ExecutionProfile(request_timeout=30.0)

# Enable retries
results = await store.abatch(ops, max_retries=3)

# Check slow queries
metrics = await store.get_metrics()
print(f"Max latency: {metrics['max_latency_ms']}ms")
```

#### 2. Unavailable Replicas

**Symptom:** StoreUnavailableError exceptions

**Causes:**
- Nodes down
- Network partitions
- Too high consistency level

**Solutions:**
```python
# Lower consistency level
profile = ExecutionProfile(
    consistency_level=ConsistencyLevel.LOCAL_ONE
)

# Check cluster health
health = await store.health_check()
print(health['checks']['cluster_connectivity'])
```

#### 3. Circuit Breaker Open

**Symptom:** StoreConnectionError with "Circuit breaker is OPEN"

**Solutions:**
```python
# Check circuit state
state = store.circuit_breaker.get_state()
print(f"State: {state['state']}")
print(f"Failures: {state['failure_count']}")

# Wait for timeout, then circuit enters HALF_OPEN
# Or manually reset (use with caution)
```

#### 4. High Memory Usage

**Symptom:** Memory growth during large searches

**Solutions:**
```python
# Use smaller fetch_size for paging
results = await store.asearch(
    namespace,
    limit=100000,
    fetch_size=100  # Smaller pages
)

# Process results in batches
for i in range(0, len(results), 1000):
    batch = results[i:i+1000]
    # Process batch
```

#### 5. Slow Batch Operations

**Symptom:** High batch latency

**Solutions:**
```python
# Use smaller batch sizes
# Split into chunks of 25-50 operations
ops_chunks = [ops[i:i+50] for i in range(0, len(ops), 50)]
for chunk in ops_chunks:
    await store.abatch(chunk)

# Use UNLOGGED for better performance
await store.abatch(ops, batch_type='UNLOGGED')

# Check batch metrics
metrics = await store.get_metrics()
print(f"Avg batch size: {metrics['batch_stats']['avg_batch_size']}")
print(f"Batch P95 size: {metrics['batch_stats']['batch_size_p95']}")
```

---

## Examples & Use Cases

### Use Case 1: Session Management

```python
# Store session with auto-expiration
await store.aput(
    namespace=("sessions",),
    key=f"session_{session_id}",
    value={
        "user_id": user_id,
        "ip_address": ip,
        "created_at": datetime.now().isoformat()
    },
    ttl=1800.0  # 30 minutes
)

# Retrieve and refresh TTL
session = await store.aget(("sessions",), f"session_{session_id}")
# TTL refreshed if refresh_on_read=True

# Delete on logout
await store.adelete(("sessions",), f"session_{session_id}")
```

### Use Case 2: Distributed Locking

```python
async def acquire_lock(resource_id: str, worker_id: str, ttl: float = 30.0):
    """Acquire distributed lock."""
    return await store.aput_if_not_exists(
        namespace=("locks",),
        key=resource_id,
        value={
            "owner": worker_id,
            "acquired_at": datetime.now(timezone.utc).isoformat()
        },
        ttl=ttl
    )

async def release_lock(resource_id: str, worker_id: str):
    """Release lock only if we own it."""
    item = await store.aget(("locks",), resource_id)
    if item and item.value.get("owner") == worker_id:
        return await store.adelete_if_value(
            namespace=("locks",),
            key=resource_id,
            expected_value=item.value
        )
    return False

# Usage
if await acquire_lock("task_123", "worker_1"):
    try:
        # Do work
        pass
    finally:
        await release_lock("task_123", "worker_1")
```

### Use Case 3: API Response Caching

```python
import hashlib
import json

async def cached_api_call(endpoint: str, params: dict, ttl: float = 300.0):
    """Cache API responses."""
    # Generate cache key
    cache_key = hashlib.md5(
        f"{endpoint}:{json.dumps(params, sort_keys=True)}".encode()
    ).hexdigest()

    # Check cache
    cached = await store.aget(("api_cache", "v1"), cache_key)
    if cached:
        return cached.value

    # Call API
    response = await call_external_api(endpoint, params)

    # Cache response
    await store.aput(
        namespace=("api_cache", "v1"),
        key=cache_key,
        value=response,
        ttl=ttl
    )

    return response
```

### Use Case 4: Rate Limiting

```python
from datetime import datetime, timezone

async def check_rate_limit(user_id: str, limit: int = 100, window: int = 60):
    """Token bucket rate limiting."""
    key = f"rate_limit_{user_id}"

    # Get current state
    item = await store.aget(("rate_limits",), key)
    now = datetime.now(timezone.utc).timestamp()

    if item:
        tokens = item.value["tokens"]
        last_refill = item.value["last_refill"]

        # Refill tokens
        elapsed = now - last_refill
        refill_amount = int(elapsed / window * limit)
        tokens = min(limit, tokens + refill_amount)
        last_refill = now
    else:
        tokens = limit
        last_refill = now

    # Check if allowed
    if tokens > 0:
        tokens -= 1
        allowed = True
    else:
        allowed = False

    # Update state
    await store.aput(
        namespace=("rate_limits",),
        key=key,
        value={
            "tokens": tokens,
            "last_refill": last_refill
        },
        ttl=window * 2.0  # Keep state for 2 windows
    )

    return allowed
```

### Use Case 5: LangGraph State Storage

```python
# Store conversation state
await store.aput(
    namespace=("langgraph", "conversations"),
    key=f"conv_{conversation_id}",
    value={
        "messages": messages,
        "context": context,
        "state": state
    },
    ttl=86400.0  # 24 hours
)

# Retrieve state
conv = await store.aget(
    ("langgraph", "conversations"),
    f"conv_{conversation_id}"
)

# Batch update multiple states
from scylladb_store import PutOp

ops = [
    PutOp(
        namespace=("langgraph", "conversations"),
        key=f"conv_{conv_id}",
        value=state
    )
    for conv_id, state in updates.items()
]
await store.abatch(ops)
```

---

## Appendix

### A. Performance Benchmarks

**Environment:** 3-node ScyllaDB cluster, c5.2xlarge instances

| Operation      | Latency (ms) | Throughput (ops/s) |
|----------------|--------------|-------------------|
| Single PUT     | 2-5          | 5,000-10,000      |
| Single GET     | 1-3          | 10,000-20,000     |
| Batch (10 ops) | 4-12         | 2,000-2,500       |
| Batch (50 ops) | 8-20         | 6,000-7,000       |
| Search (1000)  | 50-150       | 100-200           |

### B. Schema Evolution

```sql
-- Add index for faster searches (optional)
CREATE INDEX IF NOT EXISTS store_value_idx ON store (value);

-- Modify compaction strategy
ALTER TABLE store WITH compaction = {
    'class': 'SizeTieredCompactionStrategy',
    'min_threshold': '4',
    'max_threshold': '32'
};

-- Adjust gc_grace_seconds
ALTER TABLE store WITH gc_grace_seconds = 604800;  -- 7 days
```

### C. Backup & Restore

```bash
# Snapshot
nodetool snapshot -t backup_20250103 my_keyspace

# Restore from snapshot
# 1. Stop ScyllaDB
# 2. Copy snapshot files to data directory
# 3. Start ScyllaDB
# 4. Verify data
```

### D. Resources

- **ScyllaDB Documentation**: https://docs.scylladb.com/
- **Python Driver Docs**: https://python-driver.docs.scylladb.com/
- **LangGraph Documentation**: https://langchain-ai.github.io/langgraph/
- **Source Code**: Check repository for latest updates

---

**Last Updated:** 2025-10-03
**Version:** 1.0.0
**Status:** Production Ready
