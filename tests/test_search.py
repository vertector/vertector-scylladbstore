"""
Integration tests for search operations.

Tests:
- Filter-based search
- Semantic search
- Namespace listing
- Pagination
- Search with TTL refresh
"""

import pytest
import asyncio


@pytest.mark.integration
class TestFilterSearch:
    """Test filter-based search operations."""

    @pytest.mark.asyncio
    async def test_search_all_in_namespace(self, store_no_embeddings, sample_users):
        """Test searching all items in a namespace."""
        # Store sample data
        for user in sample_users:
            await store_no_embeddings.aput(**user)

        # Search all users
        results = await store_no_embeddings.asearch(("users",), limit=10)

        assert len(results) == 3
        names = {r.value["name"] for r in results}
        assert names == {"Alice Smith", "Bob Johnson", "Charlie Brown"}

    @pytest.mark.asyncio
    async def test_search_with_exact_filter(self, store_no_embeddings, sample_users):
        """Test search with exact match filter."""
        for user in sample_users:
            await store_no_embeddings.aput(**user)

        # Search for engineers only
        results = await store_no_embeddings.asearch(
            ("users",),
            filter={"role": "engineer"},
            limit=10
        )

        assert len(results) == 1
        assert results[0].value["name"] == "Alice Smith"

    @pytest.mark.asyncio
    async def test_search_with_comparison_filter(self, store_no_embeddings, sample_users):
        """Test search with comparison operators."""
        for user in sample_users:
            await store_no_embeddings.aput(**user)

        # Search for users age > 30
        results = await store_no_embeddings.asearch(
            ("users",),
            filter={"age": {"$gt": 30}},
            limit=10
        )

        assert len(results) == 1
        assert results[0].value["name"] == "Bob Johnson"
        assert results[0].value["age"] == 35

    @pytest.mark.asyncio
    async def test_search_with_multiple_filters(self, store_no_embeddings, sample_users):
        """Test search with multiple filter conditions."""
        for user in sample_users:
            await store_no_embeddings.aput(**user)

        # Search for users age >= 30 AND role = "manager"
        results = await store_no_embeddings.asearch(
            ("users",),
            filter={"age": {"$gte": 30}, "role": "manager"},
            limit=10
        )

        assert len(results) == 1
        assert results[0].value["name"] == "Bob Johnson"

    @pytest.mark.asyncio
    async def test_search_with_pagination(self, store_no_embeddings):
        """Test search with offset and limit."""
        # Create 10 items
        for i in range(10):
            await store_no_embeddings.aput(
                ("items", f"item_{i:02d}"),
                "data",
                {"index": i}
            )

        # Get first page (5 items)
        page1 = await store_no_embeddings.asearch(
            ("items",),
            limit=5,
            offset=0
        )

        assert len(page1) == 5

        # Get second page (next 5 items)
        page2 = await store_no_embeddings.asearch(
            ("items",),
            limit=5,
            offset=5
        )

        assert len(page2) == 5

        # Verify no overlap (check full identifier: namespace + key)
        page1_ids = {(item.namespace, item.key) for item in page1}
        page2_ids = {(item.namespace, item.key) for item in page2}
        assert page1_ids.isdisjoint(page2_ids)


@pytest.mark.integration
class TestSemanticSearch:
    """Test semantic search with embeddings."""

    @pytest.mark.asyncio
    async def test_semantic_search_basic(self, store_with_mock_embeddings, sample_documents):
        """Test basic semantic search."""
        # Store documents with embeddings
        for doc in sample_documents:
            await store_with_mock_embeddings.aput(
                **doc,
                wait_for_vector_sync=True
            )

        # Wait for Qdrant sync
        await asyncio.sleep(0.5)

        # Semantic search for database-related content
        results = await store_with_mock_embeddings.asearch(
            ("docs",),
            query="database systems and storage",
            limit=5
        )

        assert len(results) > 0
        # Should find the ScyllaDB document
        assert any("ScyllaDB" in r.value.get("title", "") for r in results)

    @pytest.mark.asyncio
    async def test_semantic_search_with_filters(self, store_with_mock_embeddings, sample_documents):
        """Test semantic search combined with filters."""
        for doc in sample_documents:
            await store_with_mock_embeddings.aput(
                **doc,
                wait_for_vector_sync=True
            )

        await asyncio.sleep(0.5)

        # Search for AI content only
        results = await store_with_mock_embeddings.asearch(
            ("docs",),
            query="machine learning and artificial intelligence",
            filter={"category": "ai"},
            limit=5
        )

        assert len(results) > 0
        # All results should be category=ai
        for result in results:
            assert result.value["category"] == "ai"

    @pytest.mark.asyncio
    async def test_semantic_search_score_ordering(self, store_with_mock_embeddings):
        """Test that semantic search returns results ordered by relevance."""
        # Create documents with varying relevance
        docs = [
            {
                "namespace": ("articles", "doc1"),
                "key": "content",
                "value": {"text": "Python programming language for data science"}
            },
            {
                "namespace": ("articles", "doc2"),
                "key": "content",
                "value": {"text": "JavaScript web development frontend"}
            },
            {
                "namespace": ("articles", "doc3"),
                "key": "content",
                "value": {"text": "Python machine learning and AI"}
            },
        ]

        for doc in docs:
            await store_with_mock_embeddings.aput(
                **doc,
                wait_for_vector_sync=True
            )

        await asyncio.sleep(0.5)

        # Search for Python
        results = await store_with_mock_embeddings.asearch(
            ("articles",),
            query="Python programming",
            limit=10
        )

        # Results should be ordered by score (descending)
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score


@pytest.mark.integration
class TestNamespaceListing:
    """Test namespace listing operations."""

    @pytest.mark.asyncio
    async def test_list_all_namespaces(self, store_no_embeddings):
        """Test listing all namespaces."""
        # Create items in different namespaces
        await store_no_embeddings.aput(("users", "alice"), "profile", {"name": "Alice"})
        await store_no_embeddings.aput(("users", "bob"), "profile", {"name": "Bob"})
        await store_no_embeddings.aput(("docs", "doc1"), "content", {"title": "Doc 1"})
        await store_no_embeddings.aput(("settings",), "config", {"theme": "dark"})

        namespaces = await store_no_embeddings.alist_namespaces()

        assert len(namespaces) >= 4
        assert ("users", "alice") in namespaces
        assert ("users", "bob") in namespaces
        assert ("docs", "doc1") in namespaces
        assert ("settings",) in namespaces

    @pytest.mark.asyncio
    async def test_list_namespaces_with_prefix(self, store_no_embeddings):
        """Test listing namespaces with prefix filter."""
        await store_no_embeddings.aput(("users", "alice"), "profile", {"name": "Alice"})
        await store_no_embeddings.aput(("users", "bob"), "profile", {"name": "Bob"})
        await store_no_embeddings.aput(("docs", "doc1"), "content", {"title": "Doc 1"})

        # List only user namespaces
        namespaces = await store_no_embeddings.alist_namespaces(prefix=("users",))

        assert len(namespaces) == 2
        assert all(ns[0] == "users" for ns in namespaces)

    @pytest.mark.asyncio
    async def test_list_namespaces_with_max_depth(self, store_no_embeddings):
        """Test listing namespaces with depth limit."""
        await store_no_embeddings.aput(("a", "b", "c"), "item", {"data": "nested"})
        await store_no_embeddings.aput(("a", "b"), "item", {"data": "mid"})
        await store_no_embeddings.aput(("a",), "item", {"data": "top"})

        # Get only depth 1
        namespaces = await store_no_embeddings.alist_namespaces(max_depth=1)

        # Should only return namespaces with length 1
        assert all(len(ns) == 1 for ns in namespaces)
        assert ("a",) in namespaces

    @pytest.mark.asyncio
    async def test_list_namespaces_with_suffix(self, store_no_embeddings):
        """Test listing namespaces with suffix filter."""
        await store_no_embeddings.aput(("users", "admin"), "profile", {"name": "Admin"})
        await store_no_embeddings.aput(("users", "guest"), "profile", {"name": "Guest"})
        await store_no_embeddings.aput(("docs", "admin"), "content", {"title": "Admin Doc"})

        # Find all namespaces ending with "admin"
        namespaces = await store_no_embeddings.alist_namespaces(suffix=("admin",))

        assert len(namespaces) == 2
        assert all(ns[-1] == "admin" for ns in namespaces)


@pytest.mark.integration
class TestSearchEdgeCases:
    """Test edge cases in search operations."""

    @pytest.mark.asyncio
    async def test_search_empty_namespace(self, store_no_embeddings):
        """Test searching in non-existent namespace."""
        results = await store_no_embeddings.asearch(
            ("nonexistent",),
            limit=10
        )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_with_zero_limit(self, store_no_embeddings):
        """Test search with limit=0."""
        await store_no_embeddings.aput(("test",), "item", {"data": "test"})

        results = await store_no_embeddings.asearch(
            ("test",),
            limit=0
        )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_large_offset(self, store_no_embeddings):
        """Test search with offset larger than result set."""
        # Create 5 items
        for i in range(5):
            await store_no_embeddings.aput(
                ("items",),
                f"item_{i}",
                {"index": i}
            )

        # Query with offset=100 (larger than dataset)
        results = await store_no_embeddings.asearch(
            ("items",),
            limit=10,
            offset=100
        )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_no_matching_filter(self, store_no_embeddings, sample_users):
        """Test search with filter that matches nothing."""
        for user in sample_users:
            await store_no_embeddings.aput(**user)

        results = await store_no_embeddings.asearch(
            ("users",),
            filter={"role": "ceo"},  # No CEOs in sample data
            limit=10
        )

        assert len(results) == 0
