---

## Scaling Operations

### Horizontal Scaling

#### Scale Application Pods

```bash
# Scale up
kubectl scale deployment/scylla-store --replicas=10 -n production

# Auto-scaling with HPA
kubectl autoscale deployment scylla-store \
  --cpu-percent=70 \
  --min=3 \
  --max=20 \
  -n production
```

#### Scale ScyllaDB Cluster

```bash
# Add node to cluster
# 1. Provision new node
# 2. Configure scylla.yaml with same cluster_name and seeds
# 3. Start ScyllaDB
sudo systemctl start scylla-server

# 4. Verify node joined
nodetool status

# 5. Rebalance data
nodetool repair -pr
```

#### Scale Qdrant Cluster

```bash
# Add Qdrant node (via Qdrant Cloud or manual setup)
# Qdrant will automatically distribute collections
```

### Vertical Scaling

```bash
# Update resource limits
kubectl set resources deployment scylla-store \
  --limits=cpu=4,memory=8Gi \
  --requests=cpu=2,memory=4Gi \
  -n production
```

---

## Backup and Restore

### ScyllaDB Backup

#### Full Snapshot

```bash
# Create snapshot
nodetool snapshot langgraph_store -t backup_$(date +%Y%m%d_%H%M%S)

# List snapshots
nodetool listsnapshots

# Backup snapshot to S3
aws s3 sync /var/lib/scylla/data/langgraph_store/store-*/snapshots/ \
  s3://my-backups/scylladb/snapshots/$(date +%Y%m%d)/
```

#### Automated Backup Script

```bash
#!/bin/bash
# backup.sh - Daily backup script

KEYSPACE="langgraph_store"
SNAPSHOT_NAME="auto_backup_$(date +%Y%m%d_%H%M%S)"
S3_BUCKET="s3://my-backups/scylladb"

# Create snapshot
nodetool snapshot ${KEYSPACE} -t ${SNAPSHOT_NAME}

# Upload to S3
for node in node1 node2 node3; do
  ssh ${node} "aws s3 sync /var/lib/scylla/data/${KEYSPACE}/ ${S3_BUCKET}/${node}/${SNAPSHOT_NAME}/"
done

# Clean old snapshots (keep last 7 days)
nodetool clearsnapshot -t $(date -d '7 days ago' +%Y%m%d)*

echo "Backup completed: ${SNAPSHOT_NAME}"
```

#### Restore from Snapshot

```bash
# 1. Stop application
kubectl scale deployment/scylla-store --replicas=0 -n production

# 2. Download snapshot from S3
aws s3 sync s3://my-backups/scylladb/snapshots/20231201/ \
  /var/lib/scylla/data/langgraph_store/store-*/snapshots/20231201/

# 3. Restore snapshot
nodetool refresh langgraph_store store

# 4. Verify data
cqlsh -e "SELECT COUNT(*) FROM langgraph_store.store;"

# 5. Restart application
kubectl scale deployment/scylla-store --replicas=3 -n production
```

### Qdrant Backup

```bash
# Snapshot Qdrant collection
curl -X POST "http://qdrant.internal:6333/collections/langgraph_store/snapshots" \
  -H "api-key: ${QDRANT_API_KEY}"

# Download snapshot
SNAPSHOT_NAME=$(curl "http://qdrant.internal:6333/collections/langgraph_store/snapshots" | jq -r '.[0].name')
curl -o qdrant_snapshot.tar \
  "http://qdrant.internal:6333/collections/langgraph_store/snapshots/${SNAPSHOT_NAME}"

# Upload to S3
aws s3 cp qdrant_snapshot.tar s3://my-backups/qdrant/$(date +%Y%m%d)/
```

---

## Monitoring and Alerting

### Key Metrics to Monitor

#### Application Metrics

```python
# Expose Prometheus metrics
from prometheus_client import start_http_server, Counter, Histogram, Gauge

# Start metrics server
start_http_server(9090)

# Define metrics
requests_total = Counter('scylladb_requests_total', 'Total requests', ['operation', 'status'])
latency_histogram = Histogram('scylladb_latency_seconds', 'Request latency', ['operation'])
cache_hit_rate = Gauge('scylladb_cache_hit_rate', 'Cache hit rate')
circuit_breaker_state = Gauge('scylladb_circuit_breaker_state', 'Circuit breaker state', ['name'])
```

#### Critical Alerts (PagerDuty)

```yaml
# prometheus-rules.yaml
groups:
- name: scylladb_critical
  interval: 30s
  rules:
  - alert: HighErrorRate
    expr: rate(scylladb_errors_total[5m]) > 0.01
    for: 5m
    labels:
      severity: critical
    annotations:
      summary: "High error rate detected"
      description: "Error rate is {{ $value }} errors/sec"

  - alert: CircuitBreakerOpen
    expr: scylladb_circuit_breaker_state == 2
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "Circuit breaker OPEN"

  - alert: HighLatency
    expr: histogram_quantile(0.95, scylladb_latency_seconds) > 1.0
    for: 10m
    labels:
      severity: critical
    annotations:
      summary: "P95 latency > 1s"
```

#### Warning Alerts (Slack)

```yaml
- name: scylladb_warnings
  interval: 60s
  rules:
  - alert: LowCacheHitRate
    expr: scylladb_cache_hit_rate < 0.5
    for: 15m
    labels:
      severity: warning
    annotations:
      summary: "Cache hit rate below 50%"

  - alert: HighMemoryUsage
    expr: container_memory_usage_bytes / container_spec_memory_limit_bytes > 0.8
    for: 10m
    labels:
      severity: warning
    annotations:
      summary: "Memory usage > 80%"
```

### Grafana Dashboards

```json
{
  "dashboard": {
    "title": "ScyllaDB Store Production",
    "panels": [
      {
        "title": "Request Rate",
        "targets": [
          {
            "expr": "rate(scylladb_requests_total[5m])"
          }
        ]
      },
      {
        "title": "Latency Percentiles",
        "targets": [
          {
            "expr": "histogram_quantile(0.50, scylladb_latency_seconds)",
            "legendFormat": "p50"
          },
          {
            "expr": "histogram_quantile(0.95, scylladb_latency_seconds)",
            "legendFormat": "p95"
          },
          {
            "expr": "histogram_quantile(0.99, scylladb_latency_seconds)",
            "legendFormat": "p99"
          }
        ]
      },
      {
        "title": "Error Rate",
        "targets": [
          {
            "expr": "rate(scylladb_errors_total[5m])"
          }
        ]
      },
      {
        "title": "Cache Hit Rate",
        "targets": [
          {
            "expr": "scylladb_cache_hit_rate"
          }
        ]
      }
    ]
  }
}
```

---

## Maintenance Tasks

### Daily Tasks

```bash
# 1. Check cluster health
nodetool status
kubectl get pods -n production

# 2. Review error logs
kubectl logs -n production -l app=scylla-store --since=24h | grep ERROR

# 3. Monitor metrics
# - Check Grafana dashboards
# - Verify no critical alerts
```

### Weekly Tasks

```bash
# 1. Review performance trends
# - Latency trends (p95, p99)
# - Throughput trends
# - Error rate trends

# 2. Check resource utilization
kubectl top pods -n production
kubectl top nodes

# 3. Review backup status
aws s3 ls s3://my-backups/scylladb/snapshots/ | tail -7
```

### Monthly Tasks

```bash
# 1. Repair ScyllaDB data
for node in node1 node2 node3; do
  ssh $node "nodetool repair -pr"
done

# 2. Compact SSTables
nodetool compact langgraph_store

# 3. Review and clean old data
# - Check TTL configuration
# - Verify data retention policies

# 4. Update dependencies
# - Review security patches
# - Test updates in staging
# - Schedule maintenance window
```

---

## Incident Response

### Severity Levels

- **P0 - Critical**: Complete outage, data loss risk
- **P1 - High**: Significant degradation, high error rate
- **P2 - Medium**: Partial degradation, workaround available
- **P3 - Low**: Minor issue, no customer impact

### Response Procedures

#### P0 - Complete Outage

```bash
# 1. Acknowledge incident
# 2. Assess impact
kubectl get pods -n production
kubectl logs -n production scylla-store-xxx --tail=100

# 3. Check ScyllaDB cluster
nodetool status
cqlsh -e "SELECT * FROM system.local;"

# 4. Check Qdrant
curl http://qdrant.internal:6333/health

# 5. Initiate rollback if needed (see deployment.md)

# 6. Post-incident review (within 48 hours)
```

#### P1 - High Error Rate

```bash
# 1. Identify error pattern
kubectl logs -n production -l app=scylla-store | grep ERROR | tail -100

# 2. Check circuit breaker state
curl http://scylla-store.internal/metrics | grep circuit_breaker

# 3. Scale if resource constrained
kubectl scale deployment/scylla-store --replicas=6 -n production

# 4. Enable circuit breaker if needed
# (Circuit breaker should auto-recover)

# 5. Monitor recovery
watch kubectl get pods -n production
```

---

## Disaster Recovery

### RTO/RPO Targets

- **RTO** (Recovery Time Objective): 2 hours
- **RPO** (Recovery Point Objective): 24 hours (daily backups)

### DR Procedures

#### Complete Data Center Loss

```bash
# 1. Provision new infrastructure
terraform apply -var="region=us-west-2"

# 2. Restore ScyllaDB from backup
# (Latest snapshot from S3)
aws s3 sync s3://my-backups/scylladb/snapshots/latest/ \
  /var/lib/scylla/data/

nodetool refresh langgraph_store store

# 3. Restore Qdrant from backup
curl -X POST "http://qdrant-new.internal:6333/collections/langgraph_store/snapshots/upload" \
  -F "snapshot=@qdrant_snapshot.tar"

# 4. Update DNS/load balancer to point to new cluster

# 5. Verify functionality with smoke tests

# 6. Monitor for 24 hours before declaring recovery complete
```

### Multi-Region Failover

```bash
# Primary region down - failover to DR region

# 1. Update Route53/DNS
aws route53 change-resource-record-sets \
  --hosted-zone-id Z1234567 \
  --change-batch file://failover.json

# 2. Verify DR region is healthy
curl https://scylla-store-dr.example.com/health

# 3. Monitor failover completion
watch dig scylla-store.example.com

# 4. Communicate to stakeholders
```

---

## Performance Tuning

### Application-Level Tuning

#### Connection Pool

```python
# Increase connection pool size for high throughput
from config import PoolConfig

pool_config = PoolConfig(
    core_connections=10,  # Increase from default 2
    max_connections=500,  # Increase from default 100
    executor_threads=16   # Increase from default 4
)
```

#### Batch Size

```python
# Optimize batch sizes for your workload
# Larger batches = higher throughput, higher latency
# Smaller batches = lower latency, lower throughput

await store.abatch(ops, batch_size=200)  # Adjust as needed
```

#### Cache Size

```python
# Increase embedding cache for better hit rate
store._embedding_cache = LRUCache(max_size=50000)  # Default is 10000
```

### ScyllaDB Tuning

```yaml
# /etc/scylla/scylla.yaml

# Increase concurrent operations
concurrent_reads: 128   # Default: 32
concurrent_writes: 128  # Default: 32

# Adjust compaction throughput
compaction_throughput_mb_per_sec: 256  # Default: 16

# Memory settings
memtable_flush_writers: 4  # Default: 1
```

### Qdrant Tuning

```yaml
# qdrant.yaml

# Increase HNSW parameters for better search quality
hnsw:
  m: 32              # Default: 16
  ef_construct: 200  # Default: 100

# Optimize for throughput
indexing_threshold: 50000  # Default: 20000
```

---

## Cost Optimization

### Right-Sizing

```bash
# Analyze resource utilization over 30 days
kubectl top pods -n production --containers | tee metrics.txt

# Right-size based on actual usage
# Example: If CPU usage averages 40%, reduce requests/limits
kubectl set resources deployment scylla-store \
  --limits=cpu=2,memory=4Gi \
  --requests=cpu=1,memory=2Gi \
  -n production
```

### Data Lifecycle

```sql
-- Enable TTL for temporary data
-- Reduce storage costs by auto-deleting old data
ALTER TABLE langgraph_store.store WITH default_time_to_live = 2592000;  -- 30 days
```

### Reserved Instances

```bash
# For predictable workloads, use reserved instances
# AWS: 1-year or 3-year reserved instances (up to 75% savings)
# ScyllaDB Cloud: Annual commitment (up to 30% savings)
```

---

## Compliance and Auditing

### Access Logging

```python
# Enable audit logging for all write operations
import logging

audit_logger = logging.getLogger('scylladb.audit')
audit_logger.setLevel(logging.INFO)

# Log all PUT/DELETE operations
async def aput_with_audit(self, namespace, key, value, **kwargs):
    audit_logger.info(
        f"PUT: namespace={namespace}, key={key}, user={get_current_user()}"
    )
    return await self.aput(namespace, key, value, **kwargs)
```

### Data Retention

```bash
# Configure retention policy
# Example: Keep data for 7 years for compliance

# Option 1: Use TTL
ALTER TABLE langgraph_store.store WITH default_time_to_live = 220752000;  # 7 years

# Option 2: Archive to S3 for long-term storage
# (Monthly job to export old data and compress)
```

---

## Next Steps

- [Troubleshooting Guide](troubleshooting.md) - Common issues and solutions
- [Architecture Overview](architecture.md) - System design details
- [API Reference](api.md) - Complete API documentation
