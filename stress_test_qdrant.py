"""
Comprehensive stress test for Qdrant integration with AsyncScyllaDBStore.

Tests:
1. Large-scale data ingestion (1K, 10K, 100K documents)
2. Concurrent write operations
3. High-throughput search queries
4. Performance comparison: Qdrant vs sklearn fallback
5. Memory and latency benchmarks
"""
import asyncio
import time
import random
import statistics
from datetime import datetime
from scylladb_store import AsyncScyllaDBStore
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

# Test data generators
CATEGORIES = ["Technology", "Science", "Business", "Health", "Education", "Entertainment"]
TOPICS = [
    "artificial intelligence and machine learning",
    "quantum computing and cryptography",
    "biotechnology and genetic engineering",
    "renewable energy and sustainability",
    "blockchain and distributed systems",
    "neuroscience and cognitive science",
    "robotics and automation",
    "data science and analytics",
    "cybersecurity and privacy",
    "space exploration and astronomy"
]

def generate_document(doc_id: int) -> dict:
    """Generate a synthetic document for testing."""
    topic = random.choice(TOPICS)
    category = random.choice(CATEGORIES)

    return {
        "namespace": ("stress_test", f"category_{category.lower()}", f"doc_{doc_id}"),
        "key": "content",
        "value": {
            "id": doc_id,
            "title": f"Document {doc_id}: {topic.title()}",
            "content": f"This is a comprehensive article about {topic}. " * 5,
            "category": category,
            "timestamp": datetime.now().isoformat(),
            "metadata": {
                "author": f"Author_{doc_id % 100}",
                "views": random.randint(100, 10000),
                "rating": round(random.uniform(3.0, 5.0), 1)
            }
        }
    }

async def test_ingestion_performance(store, num_docs: int, batch_size: int = 100):
    """Test data ingestion performance."""
    print(f"\n📥 Testing ingestion of {num_docs:,} documents (batch size: {batch_size})...")

    from scylladb_store import PutOp

    start_time = time.time()
    docs_inserted = 0

    for batch_start in range(0, num_docs, batch_size):
        batch_end = min(batch_start + batch_size, num_docs)

        # Create batch operations (use abatch API for better performance)
        ops = []
        for doc_id in range(batch_start, batch_end):
            doc = generate_document(doc_id)
            # Note: PutOp doesn't support wait_for_vector_sync yet
            # For bulk imports, eventual consistency is acceptable
            ops.append(PutOp(
                namespace=doc["namespace"],
                key=doc["key"],
                value=doc["value"]
            ))

        await store.abatch(ops)
        docs_inserted += len(ops)

        if docs_inserted % 1000 == 0:
            elapsed = time.time() - start_time
            rate = docs_inserted / elapsed
            print(f"   ✓ Inserted {docs_inserted:,} docs ({rate:.0f} docs/sec)")

    elapsed = time.time() - start_time
    rate = num_docs / elapsed

    print(f"   ✅ Total: {num_docs:,} docs in {elapsed:.2f}s ({rate:.0f} docs/sec)")
    return {"total_docs": num_docs, "elapsed": elapsed, "rate": rate}

async def test_search_performance(store, num_queries: int = 100, limit: int = 10):
    """Test search query performance."""
    print(f"\n🔍 Testing {num_queries} search queries (limit={limit})...")

    queries = [
        "artificial intelligence and neural networks",
        "quantum computing breakthroughs",
        "renewable energy solutions",
        "blockchain technology applications",
        "genetic engineering advances",
        "space exploration missions",
        "cybersecurity best practices",
        "data analytics tools",
        "robotics innovations",
        "cognitive neuroscience research"
    ]

    latencies = []
    results_counts = []

    for i in range(num_queries):
        query = random.choice(queries)

        start = time.time()
        results = await store.asearch(
            ("stress_test",),
            query=query,
            limit=limit
        )
        latency = (time.time() - start) * 1000  # ms

        latencies.append(latency)
        results_counts.append(len(results))

        if (i + 1) % 20 == 0:
            avg_latency = statistics.mean(latencies[-20:])
            print(f"   ✓ Completed {i + 1}/{num_queries} queries (avg: {avg_latency:.1f}ms)")

    stats = {
        "count": num_queries,
        "avg_latency_ms": statistics.mean(latencies),
        "p50_latency_ms": statistics.median(latencies),
        "p95_latency_ms": statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies),
        "p99_latency_ms": statistics.quantiles(latencies, n=100)[98] if len(latencies) >= 100 else max(latencies),
        "min_latency_ms": min(latencies),
        "max_latency_ms": max(latencies),
        "avg_results": statistics.mean(results_counts)
    }

    print(f"\n   📊 Query Performance Stats:")
    print(f"      Average latency: {stats['avg_latency_ms']:.2f}ms")
    print(f"      P50 latency: {stats['p50_latency_ms']:.2f}ms")
    print(f"      P95 latency: {stats['p95_latency_ms']:.2f}ms")
    print(f"      P99 latency: {stats['p99_latency_ms']:.2f}ms")
    print(f"      Min/Max: {stats['min_latency_ms']:.2f}ms / {stats['max_latency_ms']:.2f}ms")
    print(f"      Avg results per query: {stats['avg_results']:.1f}")

    return stats

async def test_concurrent_operations(store, num_concurrent: int = 50):
    """Test concurrent read/write operations."""
    print(f"\n🔄 Testing {num_concurrent} concurrent operations...")

    async def mixed_operation(op_id: int):
        if random.random() < 0.3:  # 30% writes
            doc = generate_document(10000 + op_id)
            await store.aput(**doc)
            return "write"
        else:  # 70% reads
            query = random.choice(TOPICS)
            await store.asearch(("stress_test",), query=query, limit=5)
            return "read"

    start = time.time()
    tasks = [mixed_operation(i) for i in range(num_concurrent)]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - start

    writes = results.count("write")
    reads = results.count("read")

    print(f"   ✅ Completed {num_concurrent} operations in {elapsed:.2f}s")
    print(f"      Writes: {writes}, Reads: {reads}")
    print(f"      Throughput: {num_concurrent / elapsed:.0f} ops/sec")

    return {"total": num_concurrent, "writes": writes, "reads": reads, "elapsed": elapsed}

async def test_filtered_search(store, num_queries: int = 50):
    """Test search with metadata filters."""
    print(f"\n🎯 Testing {num_queries} filtered search queries...")

    filters = [
        {"category": "Technology"},
        {"category": "Science"},
        {"metadata.rating": {"$gte": 4.5}},
        {"metadata.views": {"$gt": 5000}},
    ]

    latencies = []

    for i in range(num_queries):
        query = random.choice(TOPICS)
        filter_cond = random.choice(filters)

        start = time.time()
        results = await store.asearch(
            ("stress_test",),
            query=query,
            filter=filter_cond,
            limit=10
        )
        latency = (time.time() - start) * 1000
        latencies.append(latency)

        if (i + 1) % 10 == 0:
            print(f"   ✓ Completed {i + 1}/{num_queries} filtered queries")

    avg_latency = statistics.mean(latencies)
    print(f"   ✅ Average filtered query latency: {avg_latency:.2f}ms")

    return {"count": num_queries, "avg_latency_ms": avg_latency}

async def test_performance_comparison(store_with_qdrant, store_without_qdrant, num_queries: int = 20):
    """Compare Qdrant vs sklearn performance."""
    print(f"\n⚡ Performance comparison: Qdrant vs sklearn (n={num_queries})...")

    query = "artificial intelligence and machine learning applications"

    # Test with Qdrant
    qdrant_times = []
    for _ in range(num_queries):
        start = time.time()
        await store_with_qdrant.asearch(("stress_test",), query=query, limit=10)
        qdrant_times.append((time.time() - start) * 1000)

    # Test without Qdrant (sklearn fallback)
    sklearn_times = []
    for _ in range(num_queries):
        start = time.time()
        await store_without_qdrant.asearch(("stress_test",), query=query, limit=10)
        sklearn_times.append((time.time() - start) * 1000)

    qdrant_avg = statistics.mean(qdrant_times)
    sklearn_avg = statistics.mean(sklearn_times)
    speedup = sklearn_avg / qdrant_avg

    print(f"\n   📊 Results:")
    print(f"      Qdrant:  {qdrant_avg:.2f}ms (avg)")
    print(f"      sklearn: {sklearn_avg:.2f}ms (avg)")
    print(f"      Speedup: {speedup:.1f}x faster with Qdrant")

    return {
        "qdrant_avg_ms": qdrant_avg,
        "sklearn_avg_ms": sklearn_avg,
        "speedup": speedup
    }

async def cleanup_test_data(store):
    """Clean up test data."""
    print("\n🧹 Cleaning up test data...")

    # Get all stress_test namespaces
    namespaces = await store.alist_namespaces(prefix=("stress_test",))

    deleted = 0
    for ns in namespaces:
        items = await store.asearch(ns, limit=1000)
        for item in items:
            await store.adelete(item.namespace, item.key)
            deleted += 1
            if deleted % 100 == 0:
                print(f"   ✓ Deleted {deleted} items...")

    print(f"   ✅ Cleaned up {deleted} items")

async def run_stress_test():
    """Run comprehensive stress test suite."""
    print("=" * 80)
    print("🚀 QDRANT INTEGRATION STRESS TEST")
    print("=" * 80)

    # Initialize embeddings
    embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    index_config = {"dims": 768, "embed": embeddings, "fields": ["$"]}

    # Test configurations
    test_sizes = [100, 1000, 5000]  # Start small, scale up

    results = {}

    for size in test_sizes:
        print(f"\n{'=' * 80}")
        print(f"📈 TESTING WITH {size:,} DOCUMENTS")
        print(f"{'=' * 80}")

        # Store with Qdrant (mandatory)
        async with AsyncScyllaDBStore.from_contact_points(
            contact_points=["127.0.0.1"],
            keyspace="stress_test_qdrant",
            index=index_config,
            qdrant_url="http://localhost:6333",
            enable_circuit_breaker=False
        ) as store_qdrant:

            await store_qdrant.setup()

            # 1. Ingestion test
            ingestion_stats = await test_ingestion_performance(store_qdrant, size, batch_size=100)

            # 2. Search performance
            search_stats = await test_search_performance(store_qdrant, num_queries=50, limit=10)

            # 3. Concurrent operations
            concurrent_stats = await test_concurrent_operations(store_qdrant, num_concurrent=50)

            # 4. Filtered search
            filter_stats = await test_filtered_search(store_qdrant, num_queries=30)

            results[f"{size}_docs"] = {
                "ingestion": ingestion_stats,
                "search": search_stats,
                "concurrent": concurrent_stats,
                "filtered": filter_stats
            }

            # Note: sklearn comparison removed - Qdrant is now mandatory for all semantic search
            if size == test_sizes[0]:
                print("\n📝 Note: Qdrant is the only vector search option (sklearn fallback removed)")

            # Cleanup
            await cleanup_test_data(store_qdrant)

    # Summary
    print("\n" + "=" * 80)
    print("📋 STRESS TEST SUMMARY")
    print("=" * 80)

    for test_name, stats in results.items():
        print(f"\n{test_name}:")
        print(f"  {stats}")

    print("\n✅ Stress test completed successfully!")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(run_stress_test())
