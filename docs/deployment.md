# Production Deployment Guide

Complete guide for deploying AsyncScyllaDBStore to production environments.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Infrastructure Setup](#infrastructure-setup)
- [Security Configuration](#security-configuration)
- [Application Deployment](#application-deployment)
- [Post-Deployment Verification](#post-deployment-verification)
- [Rollback Procedures](#rollback-procedures)

---

## Prerequisites

### Required Services

1. **ScyllaDB Cluster** (v5.0+)
   - Minimum 3 nodes for production
   - Replication factor: 3
   - Recommended: i3.2xlarge or equivalent (8 vCPU, 61GB RAM, SSD)

2. **Qdrant Vector Database** (v1.5+)
   - Minimum 2 nodes for HA
   - Recommended: 16GB RAM, 4 vCPU per node

3. **Application Infrastructure**
   - Python 3.9+ runtime
   - Async-capable web server (Uvicorn, Hypercorn)
   - Load balancer (ALB, NGINX)

### Required Credentials

- ScyllaDB username/password (stored in secrets manager)
- TLS certificates (if using SSL)
- Qdrant API key (if authentication enabled)
- AWS/Cloud provider credentials (for secrets manager)

---

## Infrastructure Setup

### 1. ScyllaDB Cluster Setup

#### Using Docker (Development/Staging)

```bash
# docker-compose.yml
version: '3.8'

services:
  scylla:
    image: scylladb/scylla:5.4
    ports:
      - "9042:9042"
    command: --smp 2 --memory 4G
    volumes:
      - scylla_data:/var/lib/scylla
    environment:
      - SCYLLA_AUTHENTICATOR=PasswordAuthenticator
      - SCYLLA_AUTHORIZER=CassandraAuthorizer

volumes:
  scylla_data:
```

#### Using ScyllaDB Cloud (Production)

```bash
# Create cluster via ScyllaDB Cloud console
# https://cloud.scylladb.com

# Configuration:
# - Cluster name: production-cluster
# - Region: us-east-1
# - Node count: 3
# - Node type: i3.2xlarge
# - Replication factor: 3
```

#### Manual Cluster Setup

```bash
# On each ScyllaDB node (CentOS/RHEL):
sudo yum install -y scylla

# Configure scylla.yaml
sudo vi /etc/scylla/scylla.yaml

# Key settings:
# cluster_name: 'production-cluster'
# seeds: 'node1_ip,node2_ip,node3_ip'
# listen_address: <node_private_ip>
# rpc_address: <node_private_ip>
# authenticator: PasswordAuthenticator
# authorizer: CassandraAuthorizer

# Start ScyllaDB
sudo systemctl start scylla-server
sudo systemctl enable scylla-server

# Verify cluster
nodetool status
```

### 2. Qdrant Setup

#### Using Docker (Development/Staging)

```bash
# docker-compose.yml
qdrant:
  image: qdrant/qdrant:v1.7.4
  ports:
    - "6333:6333"
    - "6334:6334"
  volumes:
    - qdrant_storage:/qdrant/storage
  environment:
    - QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY}
```

#### Using Qdrant Cloud (Production)

```bash
# Create cluster via Qdrant Cloud
# https://cloud.qdrant.io

# Configuration:
# - Cluster name: production-vectors
# - Region: us-east-1
# - Node count: 2
# - Memory: 16GB per node
```

### 3. Network Configuration

#### Security Groups (AWS)

```hcl
# ScyllaDB security group
resource "aws_security_group" "scylla" {
  name = "scylla-production"

  ingress {
    from_port   = 9042
    to_port     = 9042
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]  # VPC CIDR only
  }

  ingress {
    from_port   = 7000
    to_port     = 7001
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]  # Inter-node communication
  }
}

# Qdrant security group
resource "aws_security_group" "qdrant" {
  name = "qdrant-production"

  ingress {
    from_port   = 6333
    to_port     = 6334
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]  # VPC CIDR only
  }
}
```

---

## Security Configuration

### 1. TLS/SSL Setup

#### Generate Certificates

```bash
# Using Let's Encrypt (recommended for public-facing)
certbot certonly --standalone -d scylla.example.com

# Or self-signed for internal (development only)
openssl req -x509 -newkey rsa:4096 \
  -keyout server-key.pem \
  -out server-cert.pem \
  -days 365 -nodes
```

#### Configure ScyllaDB TLS

```yaml
# /etc/scylla/scylla.yaml
client_encryption_options:
  enabled: true
  certificate: /etc/scylla/certs/server-cert.pem
  keyfile: /etc/scylla/certs/server-key.pem
  require_client_auth: true
  truststore: /etc/scylla/certs/ca-cert.pem
```

### 2. Authentication Setup

#### Create ScyllaDB User

```sql
-- Connect as default superuser
cqlsh -u cassandra -p cassandra

-- Create application user
CREATE ROLE scylla_app WITH PASSWORD = 'STRONG_PASSWORD_HERE' AND LOGIN = true;

-- Grant permissions
GRANT ALL PERMISSIONS ON KEYSPACE langgraph_store TO scylla_app;

-- Revoke default superuser (security best practice)
ALTER ROLE cassandra WITH PASSWORD = 'NEW_RANDOM_PASSWORD' AND LOGIN = false;
```

### 3. Secrets Management

#### Using AWS Secrets Manager

```bash
# Store ScyllaDB password
aws secretsmanager create-secret \
  --name prod/scylladb/password \
  --secret-string "STRONG_PASSWORD_HERE" \
  --region us-east-1

# Store Qdrant API key
aws secretsmanager create-secret \
  --name prod/qdrant/api-key \
  --secret-string "QDRANT_API_KEY_HERE" \
  --region us-east-1

# Verify
aws secretsmanager get-secret-value --secret-id prod/scylladb/password
```

#### Application IAM Role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": [
        "arn:aws:secretsmanager:us-east-1:*:secret:prod/scylladb/*",
        "arn:aws:secretsmanager:us-east-1:*:secret:prod/qdrant/*"
      ]
    }
  ]
}
```

---

## Application Deployment

### 1. Environment Configuration

Create `.env.production` file:

```bash
# ScyllaDB Configuration
SCYLLADB_CONTACT_POINTS=node1.scylla.internal,node2.scylla.internal,node3.scylla.internal
SCYLLADB_KEYSPACE=langgraph_store
SCYLLADB_PORT=9042

# Authentication
SCYLLADB_AUTH_ENABLED=true
SCYLLADB_USERNAME=scylla_app
SCYLLADB_PASSWORD_SECRET=prod/scylladb/password

# TLS Configuration
SCYLLADB_TLS_ENABLED=true
SCYLLADB_TLS_CA_CERT=/app/certs/ca-cert.pem
SCYLLADB_TLS_CERT=/app/certs/client-cert.pem
SCYLLADB_TLS_KEY=/app/certs/client-key.pem

# Qdrant Configuration
QDRANT_URL=https://qdrant.internal:6333
QDRANT_API_KEY=secret:prod/qdrant/api-key

# Secrets Provider
SECRETS_PROVIDER=aws_secrets_manager

# Observability
OTLP_ENDPOINT=https://otel-collector.internal:4317
PROMETHEUS_PORT=9090

# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_REQUESTS_PER_MINUTE=10000
```

### 2. Docker Deployment

#### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-prod.txt

# Copy application
COPY scylladb_store.py config.py observability.py rate_limiter.py ./
COPY certs/ ./certs/

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python -c "from scylladb_store import AsyncScyllaDBStore; exit(0)"

# Run application
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

#### Build and Push

```bash
# Build image
docker build -t scylla-store:v1.0.0 .

# Tag for registry
docker tag scylla-store:v1.0.0 myregistry.example.com/scylla-store:v1.0.0

# Push
docker push myregistry.example.com/scylla-store:v1.0.0
```

### 3. Kubernetes Deployment

#### Deployment Manifest

```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: scylla-store
  namespace: production
spec:
  replicas: 3
  selector:
    matchLabels:
      app: scylla-store
  template:
    metadata:
      labels:
        app: scylla-store
        version: v1.0.0
    spec:
      serviceAccountName: scylla-store
      containers:
      - name: scylla-store
        image: myregistry.example.com/scylla-store:v1.0.0
        ports:
        - containerPort: 8000
          name: http
        - containerPort: 9090
          name: metrics
        env:
        - name: SCYLLADB_CONTACT_POINTS
          valueFrom:
            configMapKeyRef:
              name: scylla-config
              key: contact_points
        - name: SCYLLADB_USERNAME
          valueFrom:
            secretKeyRef:
              name: scylla-credentials
              key: username
        - name: SCYLLADB_PASSWORD_SECRET
          value: "prod/scylladb/password"
        resources:
          requests:
            memory: "2Gi"
            cpu: "1000m"
          limits:
            memory: "4Gi"
            cpu: "2000m"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 5
        volumeMounts:
        - name: certs
          mountPath: /app/certs
          readOnly: true
      volumes:
      - name: certs
        secret:
          secretName: scylla-tls-certs
```

#### Service

```yaml
# service.yaml
apiVersion: v1
kind: Service
metadata:
  name: scylla-store
  namespace: production
spec:
  selector:
    app: scylla-store
  ports:
  - name: http
    port: 80
    targetPort: 8000
  - name: metrics
    port: 9090
    targetPort: 9090
  type: ClusterIP
```

#### Deploy

```bash
# Apply manifests
kubectl apply -f configmap.yaml
kubectl apply -f secrets.yaml
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml

# Verify deployment
kubectl rollout status deployment/scylla-store -n production
kubectl get pods -n production -l app=scylla-store
```

---

## Post-Deployment Verification

### 1. Health Checks

```bash
# Application health
curl https://scylla-store.example.com/health

# ScyllaDB connectivity
kubectl exec -it scylla-store-xxx -n production -- \
  cqlsh scylla1.internal -u scylla_app -p PASSWORD

# Qdrant connectivity
curl -H "api-key: YOUR_API_KEY" https://qdrant.internal:6333/collections
```

### 2. Smoke Tests

```python
# test_deployment.py
import asyncio
from config import load_config_from_env
from scylladb_store import AsyncScyllaDBStore

async def smoke_test():
    # Load production config
    config = load_config_from_env()
    config.resolve_secrets()

    # Create store
    async with AsyncScyllaDBStore.from_config(config) as store:
        await store.setup()

        # Test PUT
        await store.aput(
            ("smoke_test",),
            "test1",
            {"message": "deployment verification"},
            wait_for_vector_sync=True
        )

        # Test GET
        item = await store.aget(("smoke_test",), "test1")
        assert item.value["message"] == "deployment verification"

        # Test SEARCH
        results = await store.asearch(
            ("smoke_test",),
            query="verification test",
            limit=5
        )
        assert len(results) > 0

        # Cleanup
        await store.adelete(("smoke_test",), "test1")

        print("✅ All smoke tests passed!")

if __name__ == "__main__":
    asyncio.run(smoke_test())
```

### 3. Performance Validation

```bash
# Run load test (see docs/operations.md for details)
locust -f tests/load_test.py --host https://scylla-store.example.com

# Target metrics:
# - Throughput: >100 docs/sec
# - P95 latency: <500ms
# - Error rate: <0.1%
```

---

## Rollback Procedures

### Immediate Rollback (Kubernetes)

```bash
# Rollback to previous deployment
kubectl rollout undo deployment/scylla-store -n production

# Verify rollback
kubectl rollout status deployment/scylla-store -n production

# Check previous revisions
kubectl rollout history deployment/scylla-store -n production
```

### Database Schema Rollback

```bash
# If schema migration failed, restore from backup
# (Assuming you have backup from pre-deployment snapshot)

# 1. Stop application
kubectl scale deployment/scylla-store --replicas=0 -n production

# 2. Restore keyspace from snapshot
nodetool refresh langgraph_store store

# 3. Verify data
cqlsh -e "SELECT COUNT(*) FROM langgraph_store.store;"

# 4. Restart application with previous version
kubectl set image deployment/scylla-store scylla-store=myregistry.example.com/scylla-store:v0.9.0 -n production
kubectl scale deployment/scylla-store --replicas=3 -n production
```

---

## Next Steps

- [Operations Runbook](operations.md) - Day-to-day operations
- [Troubleshooting Guide](troubleshooting.md) - Common issues
- [Monitoring Setup](monitoring.md) - Observability configuration
