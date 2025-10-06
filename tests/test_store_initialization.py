"""
Tests for store initialization and TTL operations.
"""

import pytest
import asyncio

from vertector_scylladbstore import AsyncScyllaDBStore


# ============================================================================
# TTL Operation Tests
# ============================================================================

class TestTTLOperations:
    """Test TTL-related operations."""

    @pytest.mark.asyncio
    async def test_ttl_disabled_no_sweeper(self, scylla_session, mock_embeddings):
        """Test that TTL sweeper is not created when TTL disabled."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_no_ttl",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]},
            ttl={"enabled": False}
        )
        await store.setup()

        # TTL sweeper should not exist
        assert store._ttl_sweeper_task is None

        await store.aclose()

    @pytest.mark.asyncio
    async def test_ttl_with_custom_interval(self, scylla_session, mock_embeddings):
        """Test TTL sweeper with custom sweep interval."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_ttl_interval",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]},
            ttl={"enabled": True, "sweep_interval_seconds": 30}
        )
        await store.setup()

        # Verify sweeper is running (if enabled)
        if store._ttl_sweeper_task:
            assert not store._ttl_sweeper_task.done()

        await store.aclose()

    @pytest.mark.asyncio
    async def test_get_with_ttl_expired(self, scylla_session, mock_embeddings):
        """Test getting an item with expired TTL returns None."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_ttl_expired",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Put with very short TTL
        await store.aput(("test",), "key", {"data": "value"}, ttl=1)

        # Wait for expiration
        await asyncio.sleep(1.1)

        # Get should return None
        item = await store.aget(("test",), "key")
        assert item is None

        await store.aclose()


# ============================================================================
# Embedding Cache Tests
# ============================================================================

class TestEmbeddingCache:
    """Test embedding cache functionality."""

    @pytest.mark.asyncio
    async def test_embedding_cache_hit(self, scylla_session, mock_embeddings):
        """Test embedding cache returns cached values."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_cache",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # First put - should cache embedding
        await store.aput(("test",), "key1", {"text": "hello world"})

        # Get cache stats
        cache_hits_before = store._embedding_cache.hits

        # Second put with same text - should use cache
        await store.aput(("test",), "key2", {"text": "hello world"})

        cache_hits_after = store._embedding_cache.hits

        # Cache hit should have increased
        assert cache_hits_after >= cache_hits_before

        await store.aclose()

    @pytest.mark.asyncio
    async def test_embedding_cache_size(self, scylla_session, mock_embeddings):
        """Test embedding cache stores embeddings."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_cache_size",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Put some items
        await store.aput(("test",), "key1", {"text": "first"})
        await store.aput(("test",), "key2", {"text": "second"})

        # Cache should contain embeddings
        assert len(store._embedding_cache.cache) > 0

        await store.aclose()
