"""
Performance benchmark comparing optimized vs unoptimized Qdrant search.
"""
import asyncio
import time
import statistics
from scylladb_store import AsyncScyllaDBStore
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

async def setup_test_data(store, num_docs=50):
    """Setup test data."""
    print(f"Setting up {num_docs} test documents...")

    docs = [
        "Artificial intelligence and machine learning",
        "Quantum computing and cryptography",
        "Biotechnology and genetic engineering",
        "Renewable energy and sustainability",
        "Blockchain and distributed systems",
    ]

    for i in range(num_docs):
        await store.aput(
            namespace=("perf_test", f"cat_{i % 5}", f"doc_{i}"),
            key="content",
            value={
                "id": i,
                "content": docs[i % len(docs)],
                "category": f"cat_{i % 5}"
            }
        )

        if (i + 1) % 10 == 0:
            print(f"  ✓ {i + 1}/{num_docs}")

    print("✅ Setup complete\n")

async def benchmark_search(store, num_queries=20):
    """Benchmark search performance."""
    print(f"Running {num_queries} search queries...")

    queries = [
        "artificial intelligence",
        "quantum computing",
        "biotechnology",
        "renewable energy",
        "blockchain technology"
    ]

    latencies = []

    for i in range(num_queries):
        query = queries[i % len(queries)]

        start = time.time()
        results = await store.asearch(
            ("perf_test",),
            query=query,
            limit=10
        )
        elapsed = (time.time() - start) * 1000
        latencies.append(elapsed)

        if i == 0:  # Show first result details
            print(f"\n  First query results: {len(results)} found")
            if results:
                print(f"    Top result: {results[0].value.get('content', 'N/A')}")
                print(f"    Score: {results[0].score:.4f}\n")

    if latencies:
        avg = statistics.mean(latencies)
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        p99 = sorted(latencies)[int(len(latencies) * 0.99)] if len(latencies) >= 100 else max(latencies)

        print(f"\n📊 Performance Results:")
        print(f"  Queries:  {num_queries}")
        print(f"  Avg:      {avg:.1f}ms")
        print(f"  P50:      {p50:.1f}ms")
        print(f"  P95:      {p95:.1f}ms")
        print(f"  P99:      {p99:.1f}ms")
        print(f"  Min/Max:  {min(latencies):.1f}ms / {max(latencies):.1f}ms")

        return {"avg": avg, "p50": p50, "p95": p95, "p99": p99}

    return None

async def main():
    print("=" * 70)
    print("🚀 PERFORMANCE BENCHMARK - Optimized Qdrant Integration")
    print("=" * 70)

    embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    index_config = {"dims": 768, "embed": embeddings, "fields": ["$"]}

    # Test with Qdrant (mandatory)
    async with AsyncScyllaDBStore.from_contact_points(
        contact_points=["127.0.0.1"],
        keyspace="perf_benchmark",
        index=index_config,
        qdrant_url="http://localhost:6333",
        enable_circuit_breaker=False
    ) as store:

        await store.setup()

        # Setup test data
        await setup_test_data(store, num_docs=50)

        # Wait for Qdrant sync
        await asyncio.sleep(2)

        # Benchmark
        stats = await benchmark_search(store, num_queries=30)

        print(f"\n{'=' * 70}")
        print("📊 SUMMARY")
        print(f"{'=' * 70}")
        print(f"✅ Test completed with {30} queries")
        if stats:
            print(f"✅ Average latency: {stats['avg']:.1f}ms")
            print(f"✅ P95 latency: {stats['p95']:.1f}ms")
        print(f"{'=' * 70}")

if __name__ == "__main__":
    asyncio.run(main())
