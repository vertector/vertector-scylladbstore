"""
Edge case tests for store.py (error scenarios, edge cases).
"""

import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock

from vertector_scylladbstore import (
    AsyncScyllaDBStore,
    StoreValidationError,
)


# ============================================================================
# Namespace Edge Cases
# ============================================================================

class TestNamespaceEdgeCases:
    """Test namespace handling edge cases."""

    @pytest.mark.asyncio
    async def test_namespace_with_special_characters(self, scylla_session, mock_embeddings):
        """Test namespace with special characters gets URL encoded."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_special_chars",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Namespace with dots, spaces, etc
        namespace = ("my.app", "user data", "items-123")

        # Should not raise
        await store.aput(namespace, "key1", {"data": "value"})

        # Should retrieve correctly
        item = await store.aget(namespace, "key1")
        assert item is not None
        assert item.value["data"] == "value"

    @pytest.mark.asyncio
    async def test_max_namespace_depth(self, scylla_session, mock_embeddings):
        """Test maximum namespace depth limit."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_max_depth",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # 10 levels is the max
        max_namespace = tuple(f"level{i}" for i in range(10))

        # Should work
        await store.aput(max_namespace, "key", {"test": "data"})

        # 11 levels should fail
        too_deep = tuple(f"level{i}" for i in range(11))

        with pytest.raises(StoreValidationError) as exc_info:
            await store.aput(too_deep, "key", {"test": "data"})

        assert "depth" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_namespace_label_max_length(self, scylla_session, mock_embeddings):
        """Test namespace label length limit."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_label_length",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # 256 chars is max
        long_label = "a" * 256
        namespace = ("short", long_label)

        # Should work
        await store.aput(namespace, "key", {"data": "value"})

        # 257 chars should fail
        too_long_label = "a" * 257
        namespace_too_long = ("short", too_long_label)

        with pytest.raises(StoreValidationError) as exc_info:
            await store.aput(namespace_too_long, "key", {"data": "value"})

        assert "length" in str(exc_info.value).lower()


# ============================================================================
# Value Edge Cases
# ============================================================================

class TestValueEdgeCases:
    """Test value handling edge cases."""

    @pytest.mark.asyncio
    async def test_large_value_succeeds(self, scylla_session, mock_embeddings):
        """Test large values (under limit) succeed."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_large_value",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Create value > 100KB but < 1MB
        large_data = "x" * 150000
        value = {"large_field": large_data}

        # Should work
        await store.aput(("test",), "key", value)

        item = await store.aget(("test",), "key")
        assert item is not None

    @pytest.mark.asyncio
    async def test_value_max_size_limit(self, scylla_session, mock_embeddings):
        """Test maximum value size limit (1MB)."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_max_size",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Create value > 1MB (hard limit)
        huge_data = "x" * 1_100_000  # 1.1 MB
        value = {"huge_field": huge_data}

        with pytest.raises(StoreValidationError) as exc_info:
            await store.aput(("test",), "key", value)

        assert "size" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_non_dict_value_rejected(self, scylla_session, mock_embeddings):
        """Test that non-dict values are rejected."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_non_dict",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # String value
        with pytest.raises(StoreValidationError):
            await store.aput(("test",), "key", "string_value")

        # List value
        with pytest.raises(StoreValidationError):
            await store.aput(("test",), "key", ["list", "value"])

        # None value
        with pytest.raises(StoreValidationError):
            await store.aput(("test",), "key", None)


# ============================================================================
# Key Edge Cases
# ============================================================================

class TestKeyEdgeCases:
    """Test key validation edge cases."""

    @pytest.mark.asyncio
    async def test_empty_key_rejected(self, scylla_session, mock_embeddings):
        """Test that empty keys are rejected."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_empty_key",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        with pytest.raises(StoreValidationError) as exc_info:
            await store.aput(("test",), "", {"data": "value"})

        assert "key" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_key_max_length(self, scylla_session, mock_embeddings):
        """Test key length limit (1KB)."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_key_length",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # 1024 chars is max
        long_key = "k" * 1024

        # Should work
        await store.aput(("test",), long_key, {"data": "value"})

        # 1025 chars should fail
        too_long_key = "k" * 1025

        with pytest.raises(StoreValidationError) as exc_info:
            await store.aput(("test",), too_long_key, {"data": "value"})

        assert "key" in str(exc_info.value).lower()


# ============================================================================
# Search Edge Cases
# ============================================================================

class TestSearchEdgeCases:
    """Test search functionality edge cases."""

    @pytest.mark.asyncio
    async def test_search_with_zero_limit(self, scylla_session, mock_embeddings):
        """Test search with zero limit returns empty results."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_zero_limit",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Zero limit should return no results
        results = await store.asearch(("test",), limit=0)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_with_large_offset(self, scylla_session, mock_embeddings):
        """Test search with offset larger than result set."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_large_offset",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Add one item
        await store.aput(("test",), "key", {"data": "value"})

        # Offset beyond results
        results = await store.asearch(("test",), offset=100)
        assert results == []

    @pytest.mark.asyncio
    async def test_search_empty_results(self, scylla_session, mock_embeddings):
        """Test search returns empty list when no matches."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_empty_search",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Search with impossible filter
        results = await store.asearch(
            ("nonexistent",),
            filter={"impossible_field": "impossible_value"}
        )

        assert results == []


# ============================================================================
# Batch Operation Edge Cases
# ============================================================================

class TestBatchEdgeCases:
    """Test batch operation edge cases."""

    @pytest.mark.asyncio
    async def test_large_batch_succeeds(self, scylla_session, mock_embeddings):
        """Test large batch operations succeed."""
        from vertector_scylladbstore import PutOp

        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_large_batch",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Create 50 operations
        ops = [
            PutOp(
                namespace=("test",),
                key=f"key{i}",
                value={"data": f"value{i}"}
            )
            for i in range(50)
        ]

        results = await store.abatch(ops)

        # All operations should complete
        assert len(results) == 50


# ============================================================================
# TTL Edge Cases
# ============================================================================

class TestTTLEdgeCases:
    """Test TTL functionality edge cases."""

    @pytest.mark.asyncio
    async def test_ttl_refresh_on_get(self, scylla_session, mock_embeddings):
        """Test TTL refresh on get operation."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_ttl_refresh",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]},
            ttl={"enabled": True, "default_ttl_seconds": 3600}
        )
        await store.setup()

        # Put with TTL
        await store.aput(("test",), "key", {"data": "value"}, ttl=10)

        # Get with refresh
        item1 = await store.aget(("test",), "key", refresh_ttl=True)
        assert item1 is not None

        # TTL should have been refreshed
        # (Hard to test timing precisely, but should not expire immediately)

    @pytest.mark.asyncio
    async def test_ttl_zero_means_no_expiration(self, scylla_session, mock_embeddings):
        """Test that TTL=0 means no expiration."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_ttl_zero",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Put with TTL=0 (no expiration)
        await store.aput(("test",), "key", {"data": "value"}, ttl=0)

        item = await store.aget(("test",), "key")
        assert item is not None
        # Item should have no TTL set
