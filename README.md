# ScyllaDB Store for LangGraph

A ScyllaDB implementation of LangGraph's BaseStore interface, providing the same API as AsyncPostgresStore.

## Features

- ✅ Full BaseStore interface compatibility
- ✅ All AsyncPostgresStore methods implemented
- ✅ TTL (Time-To-Live) support with automatic expiration
- ✅ Hierarchical namespaces
- ✅ Filter-based search with operators ($eq, $ne, $gt, $gte, $lt, $lte)
- ✅ Batch operations for better performance
- ✅ Async context manager support
- ✅ Both sync and async APIs

## Installation

```bash
pip install -r requirements.txt
```

## Quick Start

### 1. Start ScyllaDB

```bash
docker-compose up -d
```

Wait for ScyllaDB to be ready (~30 seconds):

```bash
docker exec scylladb nodetool status
```

### 2. Use the Store

```python
import asyncio
from scylladb_store import AsyncScyllaDBStore, TTLConfig

async def main():
    # Configure TTL (optional)
    ttl_config = TTLConfig(
        refresh_on_read=True,
        default_ttl=60.0  # 60 minutes
    )

    # Create and initialize store
    async with AsyncScyllaDBStore.from_contact_points(
        contact_points=["127.0.0.1"],
        keyspace="langgraph_store",
        ttl=ttl_config
    ) as store:
        # Setup database (creates tables)
        await store.setup()

        # Store an item
        await store.aput(
            namespace=("users", "123"),
            key="profile",
            value={"name": "Alice", "email": "alice@example.com"}
        )

        # Retrieve an item
        item = await store.aget(("users", "123"), "profile")
        print(f"Retrieved: {item.value}")

        # Search items
        results = await store.asearch(
            ("users",),
            filter={"name": "Alice"},
            limit=10
        )

        # List namespaces
        namespaces = await store.alist_namespaces()
        print(f"Namespaces: {namespaces}")

asyncio.run(main())
```

## API Methods

### Async Methods (Primary API)

- `aget(namespace, key, *, refresh_ttl=None)` - Retrieve single item
- `aput(namespace, key, value, index=None, *, ttl=None)` - Store/update item
- `adelete(namespace, key)` - Delete item
- `asearch(namespace_prefix, *, query=None, filter=None, limit=10, offset=0, refresh_ttl=None)` - Search items
- `alist_namespaces(*, prefix=None, suffix=None, max_depth=None, limit=100, offset=0)` - List namespaces
- `abatch(ops)` - Execute multiple operations in batch
- `setup()` - Initialize keyspace and tables
- `sweep_ttl()` - Manual TTL cleanup (no-op for ScyllaDB - handled automatically)
- `start_ttl_sweeper()` - Start background TTL cleanup (no-op for ScyllaDB)
- `stop_ttl_sweeper()` - Stop background TTL cleanup

### Sync Methods (Thread-safe wrappers)

- `get()`, `put()`, `delete()`, `search()`, `list_namespaces()`, `batch()`

## Examples

See `example_scylladb_store.py` for comprehensive usage examples including:
- Basic CRUD operations
- Multiple items in same namespace
- Different namespaces
- Search with filters
- List namespaces
- Batch operations
- TTL support

Run the examples:

```bash
python example_scylladb_store.py
```

Run tests:

```bash
python test_scylla_store.py
```

## Configuration

### ScyllaDB (docker-compose.yml)

- Image: `scylladb/scylla:2025.3`
- CQL Port: `9042`
- REST API Port: `19000` (mapped from 10000)
- Memory: 1GB
- SMP: 1

### TTLConfig

```python
TTLConfig(
    refresh_on_read=True,  # Refresh TTL on GET/SEARCH
    default_ttl=None,  # Default TTL in minutes
    sweep_interval_minutes=None  # No-op for ScyllaDB
)
```

## Important Notes

### ScyllaDB-Specific Features

1. **Automatic TTL**: ScyllaDB handles TTL automatically via tombstones. No manual sweeping needed.
2. **No Vector Search**: Unlike PostgresStore with pgvector, vector/semantic search is not implemented.
3. **Filter Application**: Filters are applied in Python after fetching results (not at database level).

### Port Conflict Resolution

If you encounter "protocol version 127" errors, check for port conflicts:

```bash
lsof -nP -iTCP:9042 | grep LISTEN
```

Kill any conflicting processes (like Jupyter kernels) before connecting.

### Compatibility

- **Python**: 3.13+
- **ScyllaDB**: 2025.3
- **Driver**: scylla-driver 3.29.2

## Differences from AsyncPostgresStore

1. **Vector Search**: Not implemented (ScyllaDB doesn't have native vector search like pgvector)
2. **TTL Handling**: Automatic in ScyllaDB, sweep methods are no-ops
3. **Filter Performance**: Filters applied in Python, not at database level
4. **Schema**: Uses Cassandra/ScyllaDB data model (partition key: prefix, clustering key: key)

## Troubleshooting

### Connection Issues

1. **Port conflicts**: Make sure no other service is using port 9042
2. **Container not ready**: Wait for ScyllaDB to fully start (~30 seconds)
3. **Network**: Ensure Docker networking is working correctly

### Check ScyllaDB Status

```bash
# Check if running
docker ps | grep scylla

# Check logs
docker logs scylladb

# Check node status
docker exec scylladb nodetool status

# Test connection with cqlsh
docker exec scylladb cqlsh -e "SELECT release_version FROM system.local"
```

## License

This implementation follows the same license as LangGraph.
