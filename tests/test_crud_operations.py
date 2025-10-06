"""
Unit and integration tests for CRUD operations.

Tests:
- PUT operations (create/update)
- GET operations (retrieve)
- DELETE operations
- Batch operations
- TTL operations
"""

import pytest
import asyncio
from datetime import datetime


@pytest.mark.integration
class TestPutOperations:
    """Test PUT (create/update) operations."""

    @pytest.mark.asyncio
    async def test_put_simple_value(self, store_no_embeddings):
        """Test storing a simple key-value pair."""
        await store_no_embeddings.aput(
            namespace=("test",),
            key="simple",
            value={"message": "hello world"}
        )

        item = await store_no_embeddings.aget(("test",), "simple")
        assert item is not None
        assert item.value["message"] == "hello world"
        assert item.key == "simple"
        assert item.namespace == ("test",)

    @pytest.mark.asyncio
    async def test_put_nested_value(self, store_no_embeddings):
        """Test storing nested data structures."""
        value = {
            "user": {
                "name": "Alice",
                "settings": {
                    "theme": "dark",
                    "notifications": True
                }
            },
            "metadata": ["tag1", "tag2"]
        }

        await store_no_embeddings.aput(
            namespace=("users", "123"),
            key="profile",
            value=value
        )

        item = await store_no_embeddings.aget(("users", "123"), "profile")
        assert item.value["user"]["name"] == "Alice"
        assert item.value["user"]["settings"]["theme"] == "dark"
        assert item.value["metadata"] == ["tag1", "tag2"]

    @pytest.mark.asyncio
    async def test_put_update_existing(self, store_no_embeddings):
        """Test updating an existing item."""
        # Create initial version
        await store_no_embeddings.aput(
            namespace=("test",),
            key="counter",
            value={"count": 1}
        )

        # Update
        await store_no_embeddings.aput(
            namespace=("test",),
            key="counter",
            value={"count": 2}
        )

        item = await store_no_embeddings.aget(("test",), "counter")
        assert item.value["count"] == 2

    @pytest.mark.asyncio
    async def test_put_with_ttl(self, store_no_embeddings):
        """Test storing item with TTL (expires in 2 seconds)."""
        await store_no_embeddings.aput(
            namespace=("temp",),
            key="expires_soon",
            value={"data": "temporary"},
            ttl=2.0  # 2 seconds
        )

        # Item should exist immediately
        item = await store_no_embeddings.aget(("temp",), "expires_soon")
        assert item is not None

        # Wait for expiration (TTL + buffer)
        await asyncio.sleep(3.0)

        # Item should be expired/deleted
        item = await store_no_embeddings.aget(("temp",), "expires_soon")
        assert item is None

    @pytest.mark.asyncio
    async def test_put_with_embeddings(self, store_with_mock_embeddings):
        """Test storing item with automatic embedding generation."""
        await store_with_mock_embeddings.aput(
            namespace=("docs",),
            key="doc1",
            value={"content": "This is a test document about databases"},
            wait_for_vector_sync=True
        )

        # Verify stored in ScyllaDB
        item = await store_with_mock_embeddings.aget(("docs",), "doc1")
        assert item is not None
        assert item.value["content"] == "This is a test document about databases"

        # Wait for Qdrant sync
        await asyncio.sleep(0.5)

        # Verify searchable via semantic search
        results = await store_with_mock_embeddings.asearch(
            ("docs",),
            query="database systems",
            limit=5
        )
        assert len(results) > 0
        assert any(r.key == "doc1" for r in results)


@pytest.mark.integration
class TestGetOperations:
    """Test GET (retrieve) operations."""

    @pytest.mark.asyncio
    async def test_get_existing_item(self, store_no_embeddings):
        """Test retrieving an existing item."""
        await store_no_embeddings.aput(
            ("users", "alice"),
            "profile",
            {"name": "Alice", "age": 30}
        )

        item = await store_no_embeddings.aget(("users", "alice"), "profile")
        assert item is not None
        assert item.value["name"] == "Alice"
        assert item.namespace == ("users", "alice")
        assert item.key == "profile"
        assert isinstance(item.created_at, datetime)
        assert isinstance(item.updated_at, datetime)

    @pytest.mark.asyncio
    async def test_get_nonexistent_item(self, store_no_embeddings):
        """Test retrieving a non-existent item returns None."""
        item = await store_no_embeddings.aget(
            ("nonexistent",),
            "missing"
        )
        assert item is None

    @pytest.mark.asyncio
    async def test_get_with_ttl_refresh(self, store_no_embeddings):
        """Test GET refreshes TTL when configured."""
        # Store with 10-second TTL (more lenient than 5s)
        await store_no_embeddings.aput(
            ("temp",),
            "refreshable",
            {"data": "test"},
            ttl=10.0
        )

        # Wait 6 seconds
        await asyncio.sleep(6.0)

        # Get (should refresh TTL back to 10 seconds)
        item = await store_no_embeddings.aget(
            ("temp",),
            "refreshable",
            refresh_ttl=True
        )
        assert item is not None
        assert item.value["data"] == "test"

        # Wait another 6 seconds (total 12s, but TTL was refreshed at 6s)
        await asyncio.sleep(6.0)

        # Item should still exist (TTL was refreshed to 10s at the 6s mark)
        item = await store_no_embeddings.aget(("temp",), "refreshable")
        assert item is not None


@pytest.mark.integration
class TestDeleteOperations:
    """Test DELETE operations."""

    @pytest.mark.asyncio
    async def test_delete_existing_item(self, store_no_embeddings):
        """Test deleting an existing item."""
        await store_no_embeddings.aput(
            ("test",),
            "to_delete",
            {"data": "will be removed"}
        )

        # Verify exists
        item = await store_no_embeddings.aget(("test",), "to_delete")
        assert item is not None

        # Delete
        await store_no_embeddings.adelete(("test",), "to_delete")

        # Verify deleted
        item = await store_no_embeddings.aget(("test",), "to_delete")
        assert item is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_item(self, store_no_embeddings):
        """Test deleting non-existent item (should not error)."""
        # Should not raise exception
        await store_no_embeddings.adelete(("nonexistent",), "missing")

    @pytest.mark.asyncio
    async def test_delete_with_embeddings(self, store_with_mock_embeddings):
        """Test deleting item also removes from Qdrant."""
        await store_with_mock_embeddings.aput(
            ("docs",),
            "doc_to_delete",
            {"content": "This document will be deleted"},
            wait_for_vector_sync=True
        )

        # Verify in ScyllaDB
        item = await store_with_mock_embeddings.aget(("docs",), "doc_to_delete")
        assert item is not None

        # Delete
        await store_with_mock_embeddings.adelete(("docs",), "doc_to_delete")

        # Verify removed from ScyllaDB
        item = await store_with_mock_embeddings.aget(("docs",), "doc_to_delete")
        assert item is None

        # Wait for Qdrant sync
        await asyncio.sleep(0.5)

        # Verify not in search results
        results = await store_with_mock_embeddings.asearch(
            ("docs",),
            query="deleted document",
            limit=10
        )
        assert not any(r.key == "doc_to_delete" for r in results)


@pytest.mark.integration
class TestBatchOperations:
    """Test batch operations."""

    @pytest.mark.asyncio
    async def test_batch_put(self, store_no_embeddings):
        """Test batch PUT operations."""
        from vertector_scylladbstore import PutOp

        ops = [
            PutOp(
                namespace=("batch", f"item_{i}"),
                key="data",
                value={"index": i, "data": f"value_{i}"}
            )
            for i in range(5)
        ]

        results = await store_no_embeddings.abatch(ops)

        # Verify all stored
        for i in range(5):
            item = await store_no_embeddings.aget(("batch", f"item_{i}"), "data")
            assert item is not None
            assert item.value["index"] == i

    @pytest.mark.asyncio
    async def test_batch_get(self, store_no_embeddings):
        """Test batch GET operations."""
        from vertector_scylladbstore import GetOp, PutOp

        # Store some items first
        for i in range(3):
            await store_no_embeddings.aput(
                ("users", f"user_{i}"),
                "profile",
                {"name": f"User {i}"}
            )

        # Batch get
        ops = [
            GetOp(namespace=("users", f"user_{i}"), key="profile")
            for i in range(3)
        ]

        results = await store_no_embeddings.abatch(ops)

        assert len(results) == 3
        for i, item in enumerate(results):
            assert item is not None
            assert item.value["name"] == f"User {i}"

    @pytest.mark.asyncio
    async def test_batch_mixed_operations(self, store_no_embeddings):
        """Test batch with mixed operation types."""
        from vertector_scylladbstore import PutOp, GetOp, SearchOp

        # Setup: Create some items
        await store_no_embeddings.aput(
            ("users", "alice"),
            "profile",
            {"name": "Alice"}
        )

        ops = [
            # PUT new item
            PutOp(
                namespace=("users", "bob"),
                key="profile",
                value={"name": "Bob"}
            ),
            # GET existing item
            GetOp(namespace=("users", "alice"), key="profile"),
            # SEARCH
            SearchOp(namespace_prefix=("users",), limit=10),
        ]

        results = await store_no_embeddings.abatch(ops)

        assert len(results) == 3
        # PUT returns None
        assert results[0] is None
        # GET returns item
        assert results[1].value["name"] == "Alice"
        # SEARCH returns list
        assert isinstance(results[2], list)
        assert len(results[2]) >= 2  # At least Alice and Bob
