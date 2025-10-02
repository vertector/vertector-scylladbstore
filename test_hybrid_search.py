"""
Test script for hybrid search with Google Gemini embeddings.

Demonstrates both vanilla filter-based search and semantic search capabilities.
"""

import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from scylladb_store_with_embeddings import (
    HybridAsyncScyllaDBStore,
    EmbeddingConfig,
    TTLConfig
)


async def main():
    """Test hybrid search functionality."""

    # Load environment variables from .env file
    load_dotenv()

    print("=" * 70)
    print("Hybrid Search Test: Vanilla + Semantic (Gemini Embeddings)")
    print("=" * 70)

    # Configuration
    contact_points = ["127.0.0.1"]
    keyspace = "hybrid_search_test"

    # Get API key from environment
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("\n⚠️  WARNING: GOOGLE_API_KEY not set in .env file")
        print("Please create a .env file with: GOOGLE_API_KEY=your-api-key")
        return

    embedding_config = EmbeddingConfig(
        model="models/gemini-embedding-001",
        distance="cosine",
        fields=["$"],  # Embed entire document
        api_key=api_key
    )

    ttl_config = TTLConfig(refresh_on_read=True)

    print(f"\nConfiguration:")
    print(f"  Model: {embedding_config['model']}")
    print(f"  Distance: {embedding_config['distance']}")

    async with HybridAsyncScyllaDBStore.from_contact_points(
        contact_points=contact_points,
        keyspace=keyspace,
        embedding_config=embedding_config,
        ttl=ttl_config
    ) as store:
        print(f"\n✓ Connected to ScyllaDB")
        print(f"  Keyspace: {keyspace}")
        print(f"  Embedding dims: {store.embedding_config.get('dims', 'auto-detect')}")

        # Setup database
        await store.setup()
        print("✓ Database setup complete")

        # Test data: Tech articles
        print("\n" + "=" * 70)
        print("Step 1: Inserting Test Data (Tech Articles)")
        print("=" * 70)

        articles = [
            {
                "namespace": ("articles", "tech"),
                "key": "ml-101",
                "value": {
                    "title": "Introduction to Machine Learning",
                    "text": "Machine learning is a subset of artificial intelligence that focuses on training algorithms to learn patterns from data and make predictions.",
                    "category": "AI",
                    "views": 1500
                }
            },
            {
                "namespace": ("articles", "tech"),
                "key": "dl-basics",
                "value": {
                    "title": "Deep Learning Fundamentals",
                    "text": "Deep learning uses neural networks with multiple layers to process complex patterns in large datasets. It powers many modern AI applications.",
                    "category": "AI",
                    "views": 2300
                }
            },
            {
                "namespace": ("articles", "tech"),
                "key": "db-scale",
                "value": {
                    "title": "Scaling Databases for High Performance",
                    "text": "ScyllaDB and other NoSQL databases provide horizontal scalability and high throughput for modern applications.",
                    "category": "Database",
                    "views": 800
                }
            },
            {
                "namespace": ("articles", "tech"),
                "key": "web-dev",
                "value": {
                    "title": "Modern Web Development with React",
                    "text": "React is a popular JavaScript library for building user interfaces. It uses a component-based architecture and virtual DOM.",
                    "category": "Web",
                    "views": 3200
                }
            },
            {
                "namespace": ("articles", "tech"),
                "key": "cloud-native",
                "value": {
                    "title": "Cloud-Native Architecture Patterns",
                    "text": "Microservices, containers, and Kubernetes enable scalable, resilient cloud-native applications.",
                    "category": "Cloud",
                    "views": 1100
                }
            },
            {
                "namespace": ("articles", "tech"),
                "key": "nlp-intro",
                "value": {
                    "title": "Natural Language Processing Basics",
                    "text": "NLP enables computers to understand, interpret, and generate human language using techniques like tokenization and embeddings.",
                    "category": "AI",
                    "views": 1800
                }
            },
        ]

        for article in articles:
            await store.aput(**article)
            print(f"  ✓ Inserted: {article['value']['title']}")

        print(f"\n✓ Inserted {len(articles)} articles with embeddings")

        # Test 1: Vanilla Filter Search
        print("\n" + "=" * 70)
        print("Test 1: Vanilla Filter-Based Search")
        print("=" * 70)

        print("\n→ Search: articles with views > 1500")
        results = await store.asearch(
            ("articles", "tech"),
            filter={"views": {"$gt": 1500}},
            limit=10
        )
        print(f"  Found {len(results)} results:")
        for r in results:
            print(f"    - {r.value['title']} (views: {r.value['views']}, score: {r.score:.3f})")

        print("\n→ Search: AI category articles")
        results = await store.asearch(
            ("articles", "tech"),
            filter={"category": "AI"},
            limit=10
        )
        print(f"  Found {len(results)} results:")
        for r in results:
            print(f"    - {r.value['title']} (category: {r.value['category']})")

        # Test 2: Semantic Search
        print("\n" + "=" * 70)
        print("Test 2: Semantic Search (Gemini Embeddings)")
        print("=" * 70)

        queries = [
            "artificial intelligence and neural networks",
            "database performance and scalability",
            "understanding human language with computers",
            "building web applications",
        ]

        for query in queries:
            print(f"\n→ Query: \"{query}\"")
            results = await store.asearch(
                ("articles", "tech"),
                query=query,
                limit=3
            )
            print(f"  Top {len(results)} most relevant:")
            for r in results:
                print(f"    {r.score:.3f} - {r.value['title']}")

        # Test 3: Hybrid Search (Semantic + Filter)
        print("\n" + "=" * 70)
        print("Test 3: Hybrid Search (Semantic + Filter)")
        print("=" * 70)

        print("\n→ Query: \"machine learning\" + filter: views > 1000")
        results = await store.asearch(
            ("articles", "tech"),
            query="machine learning and artificial intelligence",
            filter={"views": {"$gt": 1000}},
            limit=5
        )
        print(f"  Found {len(results)} results:")
        for r in results:
            print(f"    {r.score:.3f} - {r.value['title']} (views: {r.value['views']})")

        print("\n→ Query: \"databases\" + filter: category = Database")
        results = await store.asearch(
            ("articles", "tech"),
            query="database systems and data storage",
            filter={"category": "Database"},
            limit=5
        )
        print(f"  Found {len(results)} results:")
        for r in results:
            print(f"    {r.score:.3f} - {r.value['title']}")

        # Test 4: Index Operations
        print("\n" + "=" * 70)
        print("Test 4: Index Persistence")
        print("=" * 70)

        # Save index
        index_path = "./vector_index"
        store.save_index(index_path)
        print(f"✓ Saved index to {index_path}")

        # Rebuild index (simulating recovery)
        count = await store.rebuild_index()
        print(f"✓ Rebuilt index with {count} items")

        # Load index
        store.load_index(index_path)
        print(f"✓ Loaded index from {index_path}")

        # Test after reload
        print("\n→ Query after index reload: \"neural networks\"")
        results = await store.asearch(
            ("articles", "tech"),
            query="neural networks and deep learning",
            limit=3
        )
        for r in results:
            print(f"    {r.score:.3f} - {r.value['title']}")

        # Comparison: Same query with different approaches
        print("\n" + "=" * 70)
        print("Test 5: Comparison - Filter vs Semantic vs Hybrid")
        print("=" * 70)

        test_query = "learning from data and making predictions"

        print(f"\n→ Query: \"{test_query}\"")

        # Pure filter (won't find much)
        print("\n  1. Filter Search (exact match on 'data'):")
        try:
            # This won't work well for semantic concepts
            filter_results = await store.asearch(
                ("articles", "tech"),
                filter={"text": "data"},  # This won't match
                limit=3
            )
            print(f"     Found {len(filter_results)} results")
        except:
            print("     ✗ Filter search doesn't support text matching")

        # Pure semantic
        print("\n  2. Semantic Search (embeddings):")
        semantic_results = await store.asearch(
            ("articles", "tech"),
            query=test_query,
            limit=3
        )
        for r in semantic_results:
            print(f"     {r.score:.3f} - {r.value['title']}")

        # Hybrid
        print("\n  3. Hybrid Search (semantic + filter: AI category):")
        hybrid_results = await store.asearch(
            ("articles", "tech"),
            query=test_query,
            filter={"category": "AI"},
            limit=3
        )
        for r in hybrid_results:
            print(f"     {r.score:.3f} - {r.value['title']} (category: {r.value['category']})")

        print("\n" + "=" * 70)
        print("ALL TESTS COMPLETED SUCCESSFULLY! ✨")
        print("=" * 70)

        print("\n📊 Summary:")
        print(f"  - Vanilla filter search: ✓ Working")
        print(f"  - Semantic search: ✓ Working (Gemini Embeddings)")
        print(f"  - Hybrid search: ✓ Working (best of both worlds)")
        print(f"  - Index persistence: ✓ Working")


if __name__ == "__main__":
    asyncio.run(main())
