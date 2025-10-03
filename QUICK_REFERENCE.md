# ScyllaDB Store - Quick Reference

## Installation

```bash
uv pip install scylla-driver
```

## Basic Setup

```python
from cassandra.cluster import Cluster
from scylladb_store import AsyncScyllaDBStore, TTLConfig

cluster = Cluster(['127.0.0.1'])
session = cluster.connect()

store = AsyncScyllaDBStore(
    session=session,
    keyspace='my_keyspace',
    ttl=TTLConfig(default_ttl=3600.0, refresh_on_read=True)
)
await store.setup()
```

## Core Operations

```python
# PUT - Store value
await store.aput(
    namespace=("users", "profile"),
    key="user_123",
    value={"name": "Alice", "age": 30},
    ttl=3600.0
)

# GET - Retrieve value
item = await store.aget(("users", "profile"), "user_123")

# DELETE - Remove value
await store.adelete(("users", "profile"), "user_123")

# SEARCH - Find items
results = await store.asearch(
    ("users",),
    filter={"age": 30},
    limit=100
)

# LIST NAMESPACES
namespaces = await store.alist_namespaces(prefix=("users",))
```

## Batch Operations

```python
from scylladb_store import PutOp, GetOp

# All-PUT batch (atomic)
ops = [PutOp(namespace=("users",), key=f"user_{i}", value={"id": i}) for i in range(10)]
results = await store.abatch(ops)

# LOGGED batch (full atomicity)
results = await store.abatch(ops, batch_type='LOGGED')

# Mixed operations (concurrent)
ops = [
    PutOp(namespace=("cache",), key="item1", value={"data": "new"}),
    GetOp(namespace=("cache",), key="item2"),
]
results = await store.abatch(ops)

# With retries
results = await store.abatch(
    ops,
    max_retries=3,
    retry_delay=0.1,
    retry_backoff=2.0
)
```

## Lightweight Transactions

```python
# IF NOT EXISTS (distributed lock)
success = await store.aput_if_not_exists(
    namespace=("locks",),
    key="resource_123",
    value={"owner": "worker_1"},
    ttl=30.0
)

# IF EXISTS (update only)
success = await store.aupdate_if_exists(
    namespace=("users",),
    key="user_123",
    value={"status": "inactive"}
)

# COMPARE AND SET (optimistic locking)
success = await store.acompare_and_set(
    namespace=("counters",),
    key="page_views",
    expected_value={"count": 100},
    new_value={"count": 101}
)

# DELETE IF VALUE
success = await store.adelete_if_value(
    namespace=("temp",),
    key="item",
    expected_value={"status": "done"}
)
```

## Monitoring

```python
# Health check
health = await store.health_check()
print(f"Status: {health['status']}")

# Metrics
metrics = await store.get_metrics()
print(f"Total queries: {metrics['total_queries']}")
print(f"Average latency: {metrics['avg_latency_ms']:.2f}ms")
print(f"Error rate: {metrics['error_rate']:.2%}")

# Batch metrics
print(f"Total batches: {metrics['batch_stats']['total_batches']}")
print(f"Atomic batches: {metrics['batch_stats']['atomic_batches']}")

# Prometheus export
prom_metrics = await store.export_prometheus_metrics()

# Reset metrics
await store.reset_metrics()
```

## Advanced Features

```python
# Circuit breaker
store.enable_circuit_breaker(
    failure_threshold=5,
    success_threshold=3,
    timeout_seconds=30.0
)

# Connection warmup
await store.warmup_connections(num_queries=20)

# Execution profiles
store.create_standard_profiles()

# Shard awareness info
info = store.get_shard_awareness_info()
```

## Error Handling

```python
from scylladb_store import (
    StoreValidationError,
    StoreQueryError,
    StoreTimeoutError,
    StoreUnavailableError
)

try:
    await store.aput(namespace, key, value)
except StoreValidationError as e:
    print(f"Validation error: {e.field} = {e.value}")
except StoreTimeoutError as e:
    print(f"Timeout: {e.operation_type} after {e.timeout_seconds}s")
except StoreUnavailableError as e:
    print(f"Unavailable: {e.required_replicas} replicas needed")
```

## Common Patterns

### Session Management
```python
await store.aput(
    ("sessions",), f"session_{id}",
    {"user_id": user_id, "data": data},
    ttl=1800.0
)
```

### Distributed Lock
```python
locked = await store.aput_if_not_exists(
    ("locks",), "resource",
    {"owner": "worker_1"},
    ttl=30.0
)
```

### API Caching
```python
cached = await store.aget(("api_cache",), cache_key)
if not cached:
    data = await call_api()
    await store.aput(("api_cache",), cache_key, data, ttl=300.0)
```

### Rate Limiting
```python
item = await store.aget(("rate_limits",), user_id)
if item and item.value["tokens"] > 0:
    # Allow request
    item.value["tokens"] -= 1
    await store.aput(("rate_limits",), user_id, item.value)
```

## Performance Tips

1. **Batch Size**: Keep under 50 operations
2. **Fetch Size**: Use 100-5000 for paging
3. **TTL**: Use instead of manual deletion
4. **UNLOGGED**: Default for best performance
5. **LOGGED**: Only when atomicity required
6. **Retry**: Enable for transient failures
7. **Metrics**: Monitor regularly

## Validation Limits

- **Max value size**: 1 MB
- **Max key length**: 1 KB
- **Max namespace depth**: 10 levels
- **Max batch size**: 100 operations
- **Warn batch size**: 50 operations

## Consistency Levels

- **ONE**: Fastest, least consistent
- **LOCAL_QUORUM**: Good balance (recommended)
- **QUORUM**: Strong consistency
- **ALL**: Slowest, most consistent
- **SERIAL**: For LWT operations

## Quick Troubleshooting

| Issue | Solution |
|-------|----------|
| Timeout errors | Increase request_timeout or enable retries |
| Unavailable errors | Lower consistency level or check nodes |
| Circuit breaker open | Wait for timeout or fix underlying issue |
| High memory | Use smaller fetch_size for paging |
| Slow batches | Reduce batch size to 25-50 ops |

## Key Files

- `scylladb_store.py` - Main implementation
- `SCYLLADB_STORE_GUIDE.md` - Full documentation
- `BATCH_PROCESSING_SUMMARY.md` - Batch details
- `benchmark_batch_operations.py` - Performance tool
- `test_batch_comprehensive.py` - Test suite
- `advanced_features_examples.ipynb` - Examples

---

**For detailed documentation, see SCYLLADB_STORE_GUIDE.md**
