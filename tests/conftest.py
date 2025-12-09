"""
Pytest configuration and fixtures for AsyncScyllaDBStore tests.

Provides:
- Store fixtures with cleanup
- Mock embeddings for testing
- Test data generators
- Docker container management
"""

import asyncio
import pytest
import pytest_asyncio
from typing import AsyncIterator
from cassandra.cluster import Cluster
from dotenv import load_dotenv

from vertector_scylladbstore.embeddings import QwenEmbeddings

# Load environment variables for tests
load_dotenv()


# ============================================================================
# Pytest Configuration
# ============================================================================

def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line(
        "markers", "unit: Unit tests (fast, no external dependencies)"
    )
    config.addinivalue_line(
        "markers", "integration: Integration tests (require ScyllaDB/Qdrant)"
    )
    config.addinivalue_line(
        "markers", "load: Load tests (slow, test performance)"
    )
    config.addinivalue_line(
        "markers", "chaos: Chaos tests (test failure scenarios)"
    )


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================================================
# Mock Embeddings
# ============================================================================

class MockEmbeddings:
    """Mock embeddings model for unit tests (no API calls)."""

    def __init__(self, dims: int = 768):
        self.dims = dims

    def embed_query(self, text: str) -> list[float]:
        """Generate deterministic embedding from text."""
        import hashlib
        # Use hash for deterministic results
        hash_int = int(hashlib.md5(text.encode()).hexdigest(), 16)
        # Generate normalized vector
        vector = [(hash_int >> i) % 100 / 100.0 for i in range(self.dims)]
        # Normalize
        norm = sum(x * x for x in vector) ** 0.5
        return [x / norm for x in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple documents."""
        return [self.embed_query(text) for text in texts]

    async def aembed_query(self, text: str) -> list[float]:
        """Async version of embed_query."""
        return self.embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """Async version of embed_documents."""
        return self.embed_documents(texts)


@pytest.fixture
def mock_embeddings():
    """Provide mock embeddings for unit tests."""
    return MockEmbeddings(dims=768)


@pytest.fixture(scope="session")
def real_embeddings():
    """
    Provide real embeddings for integration tests.

    Uses Qwen3 open-source model (no API key required).
    Session-scoped to avoid reloading the model for each test.
    """
    return QwenEmbeddings()


# ============================================================================
# Store Fixtures
# ============================================================================

@pytest_asyncio.fixture
async def scylla_session():
    """
    Provide ScyllaDB session for tests.

    Assumes ScyllaDB is running on localhost:9042.
    """
    cluster = Cluster(["127.0.0.1"])
    session = await asyncio.get_running_loop().run_in_executor(
        None, cluster.connect
    )

    yield session

    # Cleanup
    session.shutdown()
    cluster.shutdown()


@pytest_asyncio.fixture
async def store_no_embeddings(scylla_session):
    """
    Provide store without embeddings (for basic CRUD tests).

    Automatically cleans up test data after each test.
    """
    from vertector_scylladbstore import AsyncScyllaDBStore

    keyspace = "test_store_basic"

    store = AsyncScyllaDBStore(
        session=scylla_session,
        keyspace=keyspace,
        qdrant_url="http://localhost:6333",
        enable_circuit_breaker=False,  # Disable for tests
    )

    await store.setup()

    yield store

    # Cleanup: Drop test keyspace
    await asyncio.get_running_loop().run_in_executor(
        None,
        scylla_session.execute,
        f"DROP KEYSPACE IF EXISTS {keyspace}"
    )


@pytest_asyncio.fixture
async def store_with_mock_embeddings(scylla_session, mock_embeddings):
    """
    Provide store with mock embeddings (for unit tests).

    Uses mock embeddings to avoid API calls.
    """
    from vertector_scylladbstore import AsyncScyllaDBStore

    keyspace = "test_store_mock"

    index_config = {
        "dims": 768,
        "embed": mock_embeddings,
        "fields": ["$"]
    }

    store = AsyncScyllaDBStore(
        session=scylla_session,
        keyspace=keyspace,
        index=index_config,
        qdrant_url="http://localhost:6333",
        enable_circuit_breaker=False,
    )

    await store.setup()

    yield store

    # Cleanup
    await asyncio.get_running_loop().run_in_executor(
        None,
        scylla_session.execute,
        f"DROP KEYSPACE IF EXISTS {keyspace}"
    )

    # Cleanup Qdrant collection
    try:
        await store.qdrant_client.delete_collection(keyspace)
    except:
        pass


@pytest_asyncio.fixture
async def store_with_real_embeddings(scylla_session, real_embeddings):
    """
    Provide store with real embeddings (for integration tests).

    Uses Qwen3 open-source embeddings (no API key required).
    """
    from vertector_scylladbstore import AsyncScyllaDBStore

    keyspace = "test_store_integration"

    index_config = {
        "dims": real_embeddings.dims,  # Qwen3-Embedding-0.6B outputs 1024 dims
        "embed": real_embeddings,
        "fields": ["$"]
    }

    store = AsyncScyllaDBStore(
        session=scylla_session,
        keyspace=keyspace,
        index=index_config,
        qdrant_url="http://localhost:6333",
        enable_circuit_breaker=False,
    )

    await store.setup()

    yield store

    # Cleanup
    await asyncio.get_running_loop().run_in_executor(
        None,
        scylla_session.execute,
        f"DROP KEYSPACE IF EXISTS {keyspace}"
    )

    try:
        await store.qdrant_client.delete_collection(keyspace)
    except:
        pass


# ============================================================================
# Test Data Generators
# ============================================================================

@pytest.fixture
def sample_users():
    """Generate sample user data for tests."""
    return [
        {
            "namespace": ("users", "user_001"),
            "key": "profile",
            "value": {
                "name": "Alice Smith",
                "email": "alice@example.com",
                "age": 30,
                "role": "engineer",
                "bio": "Software engineer passionate about distributed systems"
            }
        },
        {
            "namespace": ("users", "user_002"),
            "key": "profile",
            "value": {
                "name": "Bob Johnson",
                "email": "bob@example.com",
                "age": 35,
                "role": "manager",
                "bio": "Engineering manager with focus on team productivity"
            }
        },
        {
            "namespace": ("users", "user_003"),
            "key": "profile",
            "value": {
                "name": "Charlie Brown",
                "email": "charlie@example.com",
                "age": 28,
                "role": "designer",
                "bio": "UX designer creating delightful user experiences"
            }
        },
    ]


@pytest.fixture
def sample_documents():
    """Generate sample document data for tests."""
    return [
        {
            "namespace": ("docs", "doc_001"),
            "key": "content",
            "value": {
                "title": "Introduction to ScyllaDB",
                "content": "ScyllaDB is a high-performance NoSQL database compatible with Apache Cassandra",
                "category": "database",
                "tags": ["scylladb", "nosql", "database"]
            }
        },
        {
            "namespace": ("docs", "doc_002"),
            "key": "content",
            "value": {
                "title": "Vector Databases Explained",
                "content": "Vector databases store and search high-dimensional embeddings for semantic search",
                "category": "ai",
                "tags": ["vector", "embeddings", "ai", "search"]
            }
        },
        {
            "namespace": ("docs", "doc_003"),
            "key": "content",
            "value": {
                "title": "Building LangGraph Applications",
                "content": "LangGraph enables building stateful multi-agent applications with LLMs",
                "category": "ai",
                "tags": ["langgraph", "llm", "agents"]
            }
        },
    ]


# ============================================================================
# Utility Functions
# ============================================================================

async def cleanup_namespace(store, namespace_prefix: tuple[str, ...]):
    """
    Clean up all items in a namespace.

    Args:
        store: AsyncScyllaDBStore instance
        namespace_prefix: Namespace prefix to clean
    """
    items = await store.asearch(namespace_prefix, limit=1000)
    for item in items:
        await store.adelete(item.namespace, item.key)


async def wait_for_qdrant_sync(store, delay: float = 0.5):
    """
    Wait for Qdrant sync to complete.

    Args:
        store: AsyncScyllaDBStore instance
        delay: Delay in seconds
    """
    await asyncio.sleep(delay)
