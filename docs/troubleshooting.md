# Troubleshooting Guide

Common issues and solutions for AsyncScyllaDBStore in production.

## Table of Contents

- [Connection Issues](#connection-issues)
- [Performance Problems](#performance-problems)
- [Data Consistency Issues](#data-consistency-issues)
- [Error Messages](#error-messages)
- [Debugging Tools](#debugging-tools)

---

## Connection Issues

### Issue: "NoHostAvailable" Error

**Symptoms:**
```
cassandra.cluster.NoHostAvailable: ('Unable to connect to any servers', {'127.0.0.1:9042': ConnectionRefusedError(111, "Tried connecting to [('127.0.0.1', 9042)]. Last error: Connection refused")})
```

**Causes:**
1. ScyllaDB not running
2. Firewall blocking port 9042
3. Incorrect contact points
4. Network connectivity issues

**Solutions:**

```bash
# 1. Verify ScyllaDB is running
systemctl status scylla-server
# or
docker ps | grep scylla

# 2. Check port is listening
netstat -tuln | grep 9042
# or
telnet scylla-host 9042

# 3. Verify contact points
ping scylla1.internal
nslookup scylla1.internal

# 4. Check firewall rules
sudo iptables -L | grep 9042
# or (AWS)
aws ec2 describe-security-groups --group-ids sg-xxxxx

# 5. Test with cqlsh
cqlsh scylla-host 9042 -u username -p password
```

### Issue: "Authentication Failed"

**Symptoms:**
```
cassandra.AuthenticationFailed: Error from server: code=0100 [Bad credentials] message="Provided username xxx and/or password are incorrect"
```

**Solutions:**

```bash
# 1. Verify credentials
cqlsh -u scylla_app -p 'PASSWORD_HERE'

# 2. Check secrets manager
aws secretsmanager get-secret-value --secret-id prod/scylladb/password

# 3. Verify IAM role has permissions
aws sts get-caller-identity

# 4. Check environment variables
kubectl exec -it scylla-store-xxx -n production -- env | grep SCYLLA

# 5. Test with correct credentials
python3 << EOF
from config import load_config_from_env
config = load_config_from_env()
config.resolve_secrets()
print(f"Username: {config.auth.username}")
print(f"Password: {'*' * len(config.auth.password)}")
EOF
```

### Issue: TLS/SSL Connection Failures

**Symptoms:**
```
ssl.SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed
```

**Solutions:**

```bash
# 1. Verify certificate files exist
ls -la /app/certs/
# Should see: ca-cert.pem, client-cert.pem, client-key.pem

# 2. Check certificate validity
openssl x509 -in /app/certs/client-cert.pem -noout -dates
openssl verify -CAfile /app/certs/ca-cert.pem /app/certs/client-cert.pem

# 3. Test TLS connection
openssl s_client -connect scylla-host:9042 -CAfile /app/certs/ca-cert.pem

# 4. Check certificate permissions
chmod 600 /app/certs/client-key.pem
chown app:app /app/certs/*

# 5. Verify TLS configuration
kubectl get configmap scylla-config -n production -o yaml
```

---

## Performance Problems

### Issue: High Latency (P95 > 1s)

**Symptoms:**
- Slow response times
- User complaints
- High P95/P99 latencies in metrics

**Diagnosis:**

```bash
# 1. Check Grafana dashboards
# - Latency percentiles
# - Request rate
# - Error rate

# 2. Check application logs
kubectl logs -n production scylla-store-xxx | grep "latency\|slow"

# 3. Check ScyllaDB metrics
nodetool tpstats
nodetool proxyhistograms

# 4. Check Qdrant performance
curl http://qdrant.internal:6333/metrics
```

**Solutions:**

```python
# 1. Increase connection pool
from config import PoolConfig
pool_config = PoolConfig(
    core_connections=10,
    max_connections=500
)

# 2. Increase embedding cache size
store._embedding_cache = LRUCache(max_size=50000)

# 3. Enable batch operations
# Use abatch() instead of individual aput() calls
ops = [PutOp(...) for _ in items]
await store.abatch(ops)

# 4. Optimize Qdrant queries
# Use filters to reduce search space
results = await store.asearch(
    namespace,
    query=query,
    filter={"category": "specific"},  # Narrow search
    limit=10
)

# 5. Check for slow queries
# Enable query logging
import logging
logging.getLogger('cassandra.cluster').setLevel(logging.DEBUG)
```

### Issue: Low Throughput

**Symptoms:**
- < 100 docs/sec ingestion rate
- Slow batch processing

**Solutions:**

```python
# 1. Use batch operations
ops = [PutOp(...) for item in items]
await store.abatch(ops)  # Much faster than individual puts

# 2. Use fire-and-forget for Qdrant sync
await store.aput(
    namespace, key, value,
    wait_for_vector_sync=False  # Don't block on Qdrant
)

# 3. Increase parallelism
# Process multiple batches concurrently
import asyncio
tasks = [store.abatch(batch) for batch in batches]
await asyncio.gather(*tasks)

# 4. Scale horizontally
kubectl scale deployment/scylla-store --replicas=10 -n production

# 5. Optimize embedding generation
# Ensure using async embeddings
# aembed_documents() is much faster than embed_documents()
```

### Issue: High Memory Usage

**Symptoms:**
- OOMKilled pods
- Memory usage > 80%
- Swap usage

**Solutions:**

```bash
# 1. Reduce cache size
# In scylladb_store.py
self._embedding_cache = LRUCache(max_size=5000)  # Reduce from 10000

# 2. Increase memory limits
kubectl set resources deployment scylla-store \
  --limits=memory=8Gi \
  --requests=memory=4Gi \
  -n production

# 3. Check for memory leaks
kubectl exec -it scylla-store-xxx -n production -- \
  python3 -m memory_profiler app.py

# 4. Reduce batch sizes
# Smaller batches = less memory
await store.abatch(ops, batch_size=50)  # Reduce from 100

# 5. Enable garbage collection
import gc
gc.collect()  # After large operations
```

---

## Data Consistency Issues

### Issue: Qdrant Sync Failures

**Symptoms:**
```
StoreQueryError: Failed to sync to Qdrant after 3 attempts. Data inconsistency detected.
```

**Diagnosis:**

```bash
# 1. Check Qdrant health
curl http://qdrant.internal:6333/health

# 2. Check Qdrant logs
kubectl logs -n production qdrant-xxx

# 3. Verify collection exists
curl http://qdrant.internal:6333/collections

# 4. Check network connectivity
ping qdrant.internal
telnet qdrant.internal 6333
```

**Solutions:**

```bash
# 1. Recreate Qdrant collection
curl -X DELETE http://qdrant.internal:6333/collections/langgraph_store

# Then re-initialize through application
python3 << EOF
import asyncio
from scylladb_store import AsyncScyllaDBStore
async def recreate():
    async with AsyncScyllaDBStore.from_contact_points(...) as store:
        await store.setup()  # Will recreate collection
asyncio.run(recreate())
EOF

# 2. Retry failed writes
# Use wait_for_vector_sync=True for critical data
await store.aput(
    namespace, key, value,
    wait_for_vector_sync=True  # Ensure sync completes
)

# 3. Scale Qdrant
# Add more Qdrant nodes if overloaded

# 4. Increase retry attempts
# In scylladb_store.py:_sync_to_qdrant()
max_retries = 5  # Increase from 3
```

### Issue: Search Not Finding Recently Written Data

**Symptoms:**
- PUT succeeds but SEARCH doesn't find item
- Eventual consistency delay too long

**Solutions:**

```python
# 1. Use wait_for_vector_sync for immediate consistency
await store.aput(
    namespace, key, value,
    wait_for_vector_sync=True  # Wait for Qdrant sync
)

# 2. Add delay before search (if using eventual consistency)
await store.aput(namespace, key, value)
await asyncio.sleep(0.5)  # Wait for sync
results = await store.asearch(namespace, query=...)

# 3. Verify Qdrant collection is healthy
curl http://qdrant.internal:6333/collections/langgraph_store

# 4. Check Qdrant sync logs
kubectl logs -n production scylla-store-xxx | grep "Synced vector\|Failed to sync"
```

---

## Error Messages

### "Circuit Breaker OPEN"

**Meaning:** Too many failures detected, circuit breaker protecting system.

**Solutions:**

```bash
# 1. Check what's failing
kubectl logs -n production scylla-store-xxx | grep ERROR | tail -50

# 2. Wait for auto-recovery (circuit will transition to half-open)
# Default timeout: 60 seconds

# 3. Fix underlying issue (usually database connectivity)

# 4. Manually reset if needed (requires code change)
store.circuit_breaker.reset()

# 5. Monitor recovery
curl http://scylla-store.internal/metrics | grep circuit_breaker_state
```

### "Rate Limit Exceeded"

**Meaning:** Too many requests in short time.

**Solutions:**

```python
# 1. Increase rate limit
from config import RateLimitConfig
rate_limit = RateLimitConfig(
    requests_per_minute=10000,  # Increase from default
    burst_size=1000
)

# 2. Implement backoff in client
from tenacity import retry, wait_exponential
@retry(wait=wait_exponential(multiplier=1, min=1, max=10))
async def put_with_retry():
    await store.aput(...)

# 3. Use batch operations
# Single batch of 100 items counts as 1 request
await store.abatch(ops)

# 4. Distribute load across time
# Add jitter to avoid thundering herd
await asyncio.sleep(random.uniform(0, 1))
```

### "StoreValidationError"

**Meaning:** Invalid input to store operation.

**Examples and Solutions:**

```python
# Invalid namespace (not a tuple)
await store.aput("invalid", "key", value)  # ❌
await store.aput(("valid",), "key", value)  # ✅

# Empty key
await store.aput(("ns",), "", value)  # ❌
await store.aput(("ns",), "key", value)  # ✅

# Invalid value type (not a dict)
await store.aput(("ns",), "key", "string")  # ❌
await store.aput(("ns",), "key", {"data": "string"})  # ✅

# Validate inputs
from config import ScyllaDBStoreConfig
config = ScyllaDBStoreConfig(...)  # Pydantic will validate
```

---

## Debugging Tools

### Enable Debug Logging

```python
import logging

# Application logs
logging.getLogger('scylladb_store').setLevel(logging.DEBUG)

# Cassandra driver logs
logging.getLogger('cassandra').setLevel(logging.DEBUG)

# Qdrant client logs
logging.getLogger('qdrant_client').setLevel(logging.DEBUG)

# Output to file
logging.basicConfig(
    filename='debug.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

### Performance Profiling

```python
# Use cProfile for CPU profiling
import cProfile
import pstats

profiler = cProfile.Profile()
profiler.enable()

# Your code here
await store.aput(...)

profiler.disable()
stats = pstats.Stats(profiler)
stats.sort_stats('cumulative')
stats.print_stats(20)
```

### Memory Profiling

```python
# Use memory_profiler
from memory_profiler import profile

@profile
async def memory_intensive_operation():
    # Your code here
    await store.abatch(large_ops)

# Run with: python -m memory_profiler app.py
```

### Query Analysis

```bash
# ScyllaDB slow query log
# Add to scylla.yaml:
# slow_query_log_timeout_in_ms: 500

# View slow queries
tail -f /var/log/scylla/scylla.log | grep "slow query"

# Analyze query patterns
nodetool tablehistograms langgraph_store store
```

### Network Tracing

```bash
# Capture packets for analysis
tcpdump -i eth0 -w scylla-traffic.pcap port 9042

# Analyze with wireshark
wireshark scylla-traffic.pcap

# Check connection states
ss -tan | grep 9042
netstat -an | grep 9042
```

### Distributed Tracing

```python
# Enable OpenTelemetry tracing
from observability import configure_tracing

configure_tracing(service_name="scylla-store", enabled=True)

# View traces in Jaeger/Zipkin
# http://jaeger.internal:16686
```

---

## Getting Help

### Internal Resources

1. Check runbooks: `/docs/operations.md`
2. Review architecture: `/docs/architecture.md`
3. Search incident history: Internal wiki

### External Resources

1. **ScyllaDB**:
   - Docs: https://docs.scylladb.com
   - Community: https://forum.scylladb.com
   - Slack: https://scylladb-users.slack.com

2. **Qdrant**:
   - Docs: https://qdrant.tech/documentation
   - Discord: https://qdrant.to/discord

3. **LangGraph**:
   - Docs: https://langchain-ai.github.io/langgraph
   - GitHub: https://github.com/langchain-ai/langgraph

### Escalation Path

1. **P3**: Team Slack channel
2. **P2**: On-call engineer (PagerDuty)
3. **P1**: Engineering lead + On-call
4. **P0**: All hands + Vendor support

---

## FAQ

**Q: Why is my cache hit rate low?**
A: Check if your query patterns are repetitive. LRU cache works best with repeated queries. Consider increasing cache size.

**Q: Can I use ScyllaDB without Qdrant?**
A: No, Qdrant is mandatory for semantic search. If you don't need embeddings, set `index_config=None`.

**Q: How do I migrate data from PostgreSQL?**
A: Use the migration script in `/scripts/migrate_from_postgres.py`.

**Q: What's the maximum document size?**
A: ScyllaDB limit is 2GB per row, but recommend < 1MB for performance. Use external storage (S3) for large files.

**Q: How do I handle schema changes?**
A: Use the migration system in `/migrations/`. Never ALTER tables directly in production.
