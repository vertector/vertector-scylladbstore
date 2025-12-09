# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Vertector ScyllaDB Store is a production-ready hybrid storage system combining ScyllaDB (document storage) and Qdrant (vector search). It implements LangGraph's BaseStore interface for seamless integration with LangGraph applications.

## Development Commands

```bash
# Install dependencies (use uv, not pip)
uv pip install -e ".[dev]"

# Start local services (ScyllaDB + Qdrant)
docker-compose up -d

# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_store.py -v

# Run specific test class or function
uv run pytest tests/test_store.py::TestPutOperations -v
uv run pytest tests/test_crud_operations.py::test_specific_function -v

# Run tests by marker
uv run pytest -m unit           # Fast unit tests (no external deps)
uv run pytest -m integration    # Requires ScyllaDB/Qdrant running

# Run with coverage
uv run pytest --cov=src --cov-report=term

# Linting and formatting
uv run black src/ tests/
uv run isort src/ tests/
uv run ruff check src/ tests/
uv run mypy src/
```

## Architecture

The package consists of six core modules in `src/vertector_scylladbstore/`:

- **store.py**: Core `AsyncScyllaDBStore` class implementing LangGraph's `BaseStore` interface. Contains CRUD operations (`aput`, `aget`, `adelete`, `asearch`), batch processing (`abatch`), and TTL management. Includes embedded `CircuitBreaker` and `LRUCache` classes.

- **embeddings.py**: `QwenEmbeddings` class using Qwen3-Embedding-0.6B (1024 dimensions). This is the default embedding model - no API key required.

- **config.py**: Pydantic v2 configuration models (`ScyllaDBStoreConfig`, `AuthConfig`, `TLSConfig`, `RetryConfig`, etc.). Supports loading from environment variables via `load_config_from_env()` and secrets resolution via `SecretsManager`.

- **rate_limiter.py**: Rate limiting implementations - `TokenBucketRateLimiter`, `SlidingWindowRateLimiter`, and `CompositeRateLimiter`.

- **observability.py**: OpenTelemetry tracing (`Tracer`), Prometheus metrics (`EnhancedMetrics`), percentile tracking, and alerting (`AlertManager`).

- **logging_utils.py**: Structured JSON logging with `StructuredFormatter` and `PerformanceLogger`.

## Key Patterns

### Store Initialization

Two primary patterns for creating stores:

```python
# Context manager (recommended for scripts)
async with AsyncScyllaDBStore.from_contact_points(
    contact_points=["localhost"],
    keyspace="my_app",
    qdrant_url="http://localhost:6333",
    index={}  # Enable semantic search with Qwen defaults
) as store:
    await store.aput(...)

# Direct creation (for long-lived apps/LangGraph)
store = await AsyncScyllaDBStore.create(
    contact_points=["localhost"],
    keyspace="my_app",
    qdrant_url="http://localhost:6333",
    index={}  # Enable semantic search with Qwen defaults
)
await store.setup()
# ... use store ...
await store.aclose()  # Optional cleanup
```

### Embeddings

By default, `index={}` uses `QwenEmbeddings` (Qwen3-Embedding-0.6B, 1024 dimensions). No API key required.

```python
# Default Qwen embeddings
index={}

# Specify only fields to embed
index={"fields": ["title", "content"]}

# Custom embeddings
from langchain_openai import OpenAIEmbeddings
index={"dims": 1536, "embed": OpenAIEmbeddings()}
```

### Namespace Convention

Namespaces are **tuples of strings** (not lists): `("users", "user_123")`. The MCP server uses stringified tuple notation.

### Testing

Tests require ScyllaDB and Qdrant running locally (via docker-compose). Test fixtures are in `tests/conftest.py` and provide:
- `scylla_session`: Raw Cassandra session
- `store_no_embeddings`: Basic store for CRUD tests
- `store_with_mock_embeddings`: Store with deterministic mock embeddings for unit tests
- `store_with_real_embeddings`: Store with Qwen embeddings for integration tests (no API key needed)

## Environment Variables

Key configuration variables:
- `SCYLLADB_CONTACT_POINTS`, `SCYLLADB_KEYSPACE`, `SCYLLADB_PORT`
- `QDRANT_URL`, `QDRANT_API_KEY`
- `SCYLLADB_AUTH_ENABLED`, `SCYLLADB_USERNAME`, `SCYLLADB_PASSWORD_SECRET`
- `SCYLLADB_TLS_ENABLED`, `SCYLLADB_TLS_CA_CERT`

## MCP Server

Run the built-in MCP server for AI agent integration:
```bash
uv run python -m vertector_scylladbstore
```
