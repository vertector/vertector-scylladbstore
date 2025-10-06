"""
Unit tests for connection lifecycle and initialization.

Tests:
- Store initialization
- Connection lifecycle
- Configuration validation
- Keyspace setup
- Prepared statements
"""

import pytest
from cassandra.cluster import Cluster


@pytest.mark.unit
class TestInitialization:
    """Test store initialization."""

    @pytest.mark.asyncio
    async def test_init_without_embeddings(self, scylla_session):
        """Test initialization without embeddings."""
        from vertector_scylladbstore import AsyncScyllaDBStore

        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_init_basic",
            qdrant_url="http://localhost:6333",
        )

        assert store.keyspace == "test_init_basic"
        assert store.session == scylla_session
        assert store.index_config is None
        assert store.qdrant_client is not None

    @pytest.mark.asyncio
    async def test_init_with_embeddings(self, scylla_session, mock_embeddings):
        """Test initialization with embeddings."""
        from vertector_scylladbstore import AsyncScyllaDBStore

        index_config = {
            "dims": 768,
            "embed": mock_embeddings,
            "fields": ["$"]
        }

        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_init_embeddings",
            index=index_config,
            qdrant_url="http://localhost:6333",
        )

        assert store.index_config is not None
        assert store.index_config["dims"] == 768
        assert store.index_config["embed"] == mock_embeddings

    @pytest.mark.asyncio
    async def test_init_with_circuit_breaker(self, scylla_session):
        """Test initialization with circuit breaker enabled."""
        from vertector_scylladbstore import AsyncScyllaDBStore

        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_init_cb",
            qdrant_url="http://localhost:6333",
            enable_circuit_breaker=True,
            circuit_breaker_config={
                "failure_threshold": 3,
                "success_threshold": 2,
                "timeout_seconds": 30.0
            }
        )

        assert store.circuit_breaker is not None
        assert store.circuit_breaker.failure_threshold == 3
        assert store.circuit_breaker.success_threshold == 2
        assert store.circuit_breaker.timeout_seconds == 30.0


@pytest.mark.integration
class TestSetup:
    """Test database setup operations."""

    @pytest.mark.asyncio
    async def test_setup_creates_keyspace(self, scylla_session):
        """Test that setup creates keyspace."""
        from vertector_scylladbstore import AsyncScyllaDBStore
        import asyncio

        keyspace = "test_setup_keyspace"
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace=keyspace,
            qdrant_url="http://localhost:6333",
        )

        await store.setup()

        # Verify keyspace exists
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            scylla_session.execute,
            f"SELECT keyspace_name FROM system_schema.keyspaces WHERE keyspace_name = '{keyspace}'"
        )

        rows = list(result)
        assert len(rows) == 1
        assert rows[0].keyspace_name == keyspace

        # Cleanup
        await asyncio.get_running_loop().run_in_executor(
            None,
            scylla_session.execute,
            f"DROP KEYSPACE IF EXISTS {keyspace}"
        )

    @pytest.mark.asyncio
    async def test_setup_creates_table(self, scylla_session):
        """Test that setup creates store table."""
        from vertector_scylladbstore import AsyncScyllaDBStore
        import asyncio

        keyspace = "test_setup_table"
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace=keyspace,
            qdrant_url="http://localhost:6333",
        )

        await store.setup()

        # Verify table exists
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            scylla_session.execute,
            f"SELECT table_name FROM system_schema.tables WHERE keyspace_name = '{keyspace}' AND table_name = 'store'"
        )

        rows = list(result)
        assert len(rows) == 1
        assert rows[0].table_name == "store"

        # Cleanup
        await asyncio.get_running_loop().run_in_executor(
            None,
            scylla_session.execute,
            f"DROP KEYSPACE IF EXISTS {keyspace}"
        )

    @pytest.mark.asyncio
    async def test_setup_creates_qdrant_collection(self, scylla_session, mock_embeddings):
        """Test that setup creates Qdrant collection when embeddings configured."""
        from vertector_scylladbstore import AsyncScyllaDBStore

        keyspace = "test_setup_qdrant"

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
        )

        await store.setup()

        # Verify Qdrant collection exists
        collections = await store.qdrant_client.get_collections()
        collection_names = [c.name for c in collections.collections]

        assert keyspace in collection_names

        # Cleanup
        import asyncio
        await asyncio.get_running_loop().run_in_executor(
            None,
            scylla_session.execute,
            f"DROP KEYSPACE IF EXISTS {keyspace}"
        )
        await store.qdrant_client.delete_collection(keyspace)


@pytest.mark.integration
class TestConnectionLifecycle:
    """Test connection lifecycle management."""

    @pytest.mark.asyncio
    async def test_context_manager_lifecycle(self):
        """Test store lifecycle with context manager."""
        from vertector_scylladbstore import AsyncScyllaDBStore

        keyspace = "test_context_manager"

        async with AsyncScyllaDBStore.from_contact_points(
            contact_points=["127.0.0.1"],
            keyspace=keyspace,
            qdrant_url="http://localhost:6333",
        ) as store:
            await store.setup()

            # Store should be usable
            await store.aput(
                namespace=("test",),
                key="item1",
                value={"data": "test"}
            )

            item = await store.aget(("test",), "item1")
            assert item is not None
            assert item.value["data"] == "test"

        # After context exits, cleanup should have happened
        # (Note: We can't test session shutdown as it's handled by cassandra-driver)

    @pytest.mark.asyncio
    async def test_explicit_lifecycle(self, scylla_session):
        """Test explicit initialization without context manager."""
        from vertector_scylladbstore import AsyncScyllaDBStore
        import asyncio

        keyspace = "test_explicit_lifecycle"

        # Create store (simulating LangGraph usage)
        store_cm = AsyncScyllaDBStore.from_contact_points(
            contact_points=["127.0.0.1"],
            keyspace=keyspace,
            qdrant_url="http://localhost:6333",
        )

        store = await store_cm.__aenter__()
        await store.setup()

        # Use store
        await store.aput(
            namespace=("test",),
            key="item1",
            value={"data": "test"}
        )

        item = await store.aget(("test",), "item1")
        assert item is not None

        # Cleanup
        await asyncio.get_running_loop().run_in_executor(
            None,
            scylla_session.execute,
            f"DROP KEYSPACE IF EXISTS {keyspace}"
        )
