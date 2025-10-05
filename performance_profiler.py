"""
Performance profiler to identify bottlenecks in ScyllaDB + Qdrant operations.
"""
import asyncio
import time
from scylladb_store import AsyncScyllaDBStore
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

class PerformanceProfiler:
    def __init__(self):
        self.timings = {}

    def record(self, operation: str, duration_ms: float):
        if operation not in self.timings:
            self.timings[operation] = []
        self.timings[operation].append(duration_ms)

    def report(self):
        print("\n" + "="*80)
        print("🔍 PERFORMANCE PROFILING REPORT")
        print("="*80)

        for op, times in sorted(self.timings.items()):
            avg = sum(times) / len(times)
            min_t = min(times)
            max_t = max(times)
            total = sum(times)
            print(f"\n{op}:")
            print(f"  Count: {len(times)}")
            print(f"  Avg:   {avg:.1f}ms")
            print(f"  Min:   {min_t:.1f}ms")
            print(f"  Max:   {max_t:.1f}ms")
            print(f"  Total: {total:.1f}ms")

profiler = PerformanceProfiler()

async def profile_write_operation(store):
    """Profile a single write operation."""
    print("\n📝 Profiling WRITE operation...")

    # 1. Embedding generation
    start = time.time()
    embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    test_text = "Artificial intelligence and machine learning"
    embedding = embeddings.embed_query(test_text)
    embed_time = (time.time() - start) * 1000
    profiler.record("embedding_generation", embed_time)
    print(f"  ✓ Embedding generation: {embed_time:.1f}ms")

    # 2. Database write
    start = time.time()
    await store.aput(
        namespace=("profile", "test"),
        key="doc_1",
        value={"content": test_text, "id": 1}
    )
    write_time = (time.time() - start) * 1000
    profiler.record("scylladb_write", write_time - embed_time)  # Subtract embedding time
    print(f"  ✓ ScyllaDB write: {write_time - embed_time:.1f}ms")
    print(f"  ✓ Total write: {write_time:.1f}ms")

async def profile_search_operation(store):
    """Profile a single search operation."""
    print("\n🔍 Profiling SEARCH operation...")

    # 1. Query embedding
    start = time.time()
    embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    embedding = embeddings.embed_query("artificial intelligence")
    embed_time = (time.time() - start) * 1000
    profiler.record("search_embedding_generation", embed_time)
    print(f"  ✓ Query embedding: {embed_time:.1f}ms")

    # 2. Search operation
    start = time.time()
    results = await store.asearch(
        ("profile",),
        query="artificial intelligence",
        limit=10
    )
    search_time = (time.time() - start) * 1000
    profiler.record("full_search", search_time)
    print(f"  ✓ Full search: {search_time:.1f}ms")
    print(f"  ✓ Search only (no embed): {search_time - embed_time:.1f}ms")
    print(f"  ✓ Results found: {len(results)}")

async def profile_batch_writes(store, count=10):
    """Profile batch write operations."""
    print(f"\n📦 Profiling BATCH writes ({count} documents)...")

    # Create batch operations
    from scylladb_store import PutOp
    ops = [
        PutOp(
            namespace=("profile", "batch"),
            key=f"doc_{i}",
            value={"content": f"Document {i} about technology", "id": i}
        )
        for i in range(count)
    ]

    start = time.time()
    await store.abatch(ops)
    total_time = (time.time() - start) * 1000
    avg_per_doc = total_time / count

    profiler.record("batch_write_total", total_time)
    profiler.record("avg_write_per_doc", avg_per_doc)

    print(f"  ✓ Total time: {total_time:.1f}ms")
    print(f"  ✓ Avg per doc: {avg_per_doc:.1f}ms")
    print(f"  ✓ Throughput: {count / (total_time/1000):.1f} docs/sec")

async def check_qdrant_latency(store):
    """Check Qdrant connection latency."""
    print("\n🌐 Checking QDRANT latency...")

    try:
        start = time.time()
        collections = await store.qdrant_client.get_collections()
        latency = (time.time() - start) * 1000
        profiler.record("qdrant_health_check", latency)
        print(f"  ✓ Qdrant health check: {latency:.1f}ms")
        print(f"  ✓ Collections: {len(collections.collections)}")
    except Exception as e:
        print(f"  ✗ Qdrant error: {e}")

async def check_scylladb_latency(store):
    """Check ScyllaDB connection latency."""
    print("\n💾 Checking SCYLLADB latency...")

    # Simple query
    start = time.time()
    result = await store.aget(("profile", "test"), "doc_1")
    latency = (time.time() - start) * 1000
    profiler.record("scylladb_simple_get", latency)
    print(f"  ✓ Simple GET: {latency:.1f}ms")

async def cleanup_test_data(store):
    """Clean up test data before profiling."""
    print("\n🧹 Cleaning up previous test data...")

    try:
        # Delete all test namespaces
        namespaces = await store.alist_namespaces(prefix=("profile",))
        deleted = 0

        for ns in namespaces:
            # Search for all items in namespace
            items = await store.asearch(ns, limit=1000)
            for item in items:
                await store.adelete(item.namespace, item.key)
                deleted += 1

        if deleted > 0:
            print(f"  ✓ Cleaned up {deleted} items")
        else:
            print(f"  ✓ No previous data found")
    except Exception as e:
        print(f"  ⚠️  Cleanup warning: {e}")

async def main():
    print("="*80)
    print("🚀 PERFORMANCE PROFILING - Finding Bottlenecks")
    print("="*80)

    embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    index_config = {"dims": 768, "embed": embeddings, "fields": ["$"]}

    async with AsyncScyllaDBStore.from_contact_points(
        contact_points=["127.0.0.1"],
        keyspace="profiling",
        index=index_config,
        qdrant_url="http://localhost:6333",
        enable_circuit_breaker=False
    ) as store:

        await store.setup()

        # Clean up previous test data
        await cleanup_test_data(store)

        # Run profiling tests
        await check_qdrant_latency(store)
        await check_scylladb_latency(store)
        await profile_write_operation(store)
        await profile_search_operation(store)
        await profile_batch_writes(store, count=10)

        # Generate report
        profiler.report()

        # Analysis
        print("\n" + "="*80)
        print("📊 BOTTLENECK ANALYSIS")
        print("="*80)

        if "embedding_generation" in profiler.timings:
            avg_embed = sum(profiler.timings["embedding_generation"]) / len(profiler.timings["embedding_generation"])
            print(f"\n⚠️  Embedding generation: {avg_embed:.1f}ms avg")
            if avg_embed > 100:
                print("   → BOTTLENECK: Embedding API calls are slow")
                print("   → FIX: Use batch embedding, caching, or local model")

        if "full_search" in profiler.timings:
            avg_search = sum(profiler.timings["full_search"]) / len(profiler.timings["full_search"])
            print(f"\n⚠️  Full search operation: {avg_search:.1f}ms avg")
            if avg_search > 50:
                print("   → BOTTLENECK: Search is slower than expected")
                print("   → CHECK: Qdrant sync, network latency, or query complexity")

        # Show cache statistics
        print("\n" + "="*80)
        print("📈 EMBEDDING CACHE STATISTICS")
        print("="*80)
        cache_stats = store.get_embedding_cache_stats()
        print(f"\nCache size: {cache_stats['size']}/{cache_stats['max_size']}")
        print(f"Cache hits: {cache_stats['hits']}")
        print(f"Cache misses: {cache_stats['misses']}")
        print(f"Hit rate: {cache_stats['hit_rate']:.1%}")

        # Clean up after tests
        await cleanup_test_data(store)

if __name__ == "__main__":
    asyncio.run(main())
