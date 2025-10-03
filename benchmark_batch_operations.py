#!/usr/bin/env python3
"""
Performance Benchmark: LOGGED vs UNLOGGED Batch Operations

Measures throughput, latency, and resource usage for different batch types.
"""

import asyncio
import time
import statistics
from typing import List, Dict, Any
from cassandra.cluster import Cluster
from scylladb_store import AsyncScyllaDBStore, PutOp, TTLConfig


class BatchBenchmark:
    """Benchmark batch operations with different configurations."""

    def __init__(self, contact_points: List[str], keyspace: str):
        self.contact_points = contact_points
        self.keyspace = keyspace
        self.store = None

    async def setup(self):
        """Initialize connection to ScyllaDB."""
        cluster = Cluster(self.contact_points)
        session = cluster.connect()

        self.store = AsyncScyllaDBStore(
            session=session,
            keyspace=self.keyspace,
            ttl=TTLConfig(refresh_on_read=False)
        )

        await self.store.setup()
        print(f"✓ Connected to ScyllaDB at {self.contact_points}")
        print(f"✓ Using keyspace: {self.keyspace}\n")

    async def benchmark_batch_type(
        self,
        batch_type: str,
        batch_sizes: List[int],
        num_iterations: int = 10
    ) -> Dict[str, Any]:
        """
        Benchmark a specific batch type across different batch sizes.

        Args:
            batch_type: "LOGGED" or "UNLOGGED"
            batch_sizes: List of batch sizes to test
            num_iterations: Number of iterations per batch size

        Returns:
            Dictionary with benchmark results
        """
        print(f"{'='*70}")
        print(f"Benchmarking {batch_type} Batch")
        print(f"{'='*70}\n")

        results = {
            'batch_type': batch_type,
            'batch_sizes': {},
        }

        for batch_size in batch_sizes:
            print(f"Testing batch size: {batch_size}")

            latencies = []
            throughputs = []

            for iteration in range(num_iterations):
                # Create batch operations
                ops = [
                    PutOp(
                        namespace=('benchmark', batch_type.lower()),
                        key=f'item_{iteration}_{i}',
                        value={'id': i, 'iteration': iteration, 'data': f'test_{i}'}
                    )
                    for i in range(batch_size)
                ]

                # Measure execution time
                start_time = time.perf_counter()
                await self.store.abatch(ops, batch_type=batch_type)
                end_time = time.perf_counter()

                # Calculate metrics
                latency_ms = (end_time - start_time) * 1000
                throughput = batch_size / (end_time - start_time)  # ops/sec

                latencies.append(latency_ms)
                throughputs.append(throughput)

                # Small delay between iterations
                await asyncio.sleep(0.1)

            # Calculate statistics
            results['batch_sizes'][batch_size] = {
                'latency_ms': {
                    'min': min(latencies),
                    'max': max(latencies),
                    'mean': statistics.mean(latencies),
                    'median': statistics.median(latencies),
                    'stdev': statistics.stdev(latencies) if len(latencies) > 1 else 0,
                    'p95': sorted(latencies)[int(0.95 * len(latencies))],
                    'p99': sorted(latencies)[int(0.99 * len(latencies))],
                },
                'throughput_ops_sec': {
                    'min': min(throughputs),
                    'max': max(throughputs),
                    'mean': statistics.mean(throughputs),
                    'median': statistics.median(throughputs),
                },
            }

            print(f"  Latency (mean): {results['batch_sizes'][batch_size]['latency_ms']['mean']:.2f}ms")
            print(f"  Throughput (mean): {results['batch_sizes'][batch_size]['throughput_ops_sec']['mean']:.0f} ops/sec\n")

        return results

    def print_comparison(self, unlogged_results: Dict, logged_results: Dict):
        """Print side-by-side comparison of results."""
        print(f"\n{'='*70}")
        print("PERFORMANCE COMPARISON: UNLOGGED vs LOGGED")
        print(f"{'='*70}\n")

        print(f"{'Batch Size':<12} {'UNLOGGED (ms)':<20} {'LOGGED (ms)':<20} {'Speedup':<10}")
        print(f"{'-'*70}")

        for batch_size in unlogged_results['batch_sizes'].keys():
            unlogged_mean = unlogged_results['batch_sizes'][batch_size]['latency_ms']['mean']
            logged_mean = logged_results['batch_sizes'][batch_size]['latency_ms']['mean']
            speedup = logged_mean / unlogged_mean

            print(f"{batch_size:<12} {unlogged_mean:<20.2f} {logged_mean:<20.2f} {speedup:<10.2f}x")

        print(f"\n{'='*70}")
        print("THROUGHPUT COMPARISON (ops/sec)")
        print(f"{'='*70}\n")

        print(f"{'Batch Size':<12} {'UNLOGGED':<20} {'LOGGED':<20} {'Difference':<15}")
        print(f"{'-'*70}")

        for batch_size in unlogged_results['batch_sizes'].keys():
            unlogged_tput = unlogged_results['batch_sizes'][batch_size]['throughput_ops_sec']['mean']
            logged_tput = logged_results['batch_sizes'][batch_size]['throughput_ops_sec']['mean']
            diff_pct = ((unlogged_tput - logged_tput) / logged_tput) * 100

            print(f"{batch_size:<12} {unlogged_tput:<20.0f} {logged_tput:<20.0f} {diff_pct:>+14.1f}%")

    def print_detailed_stats(self, results: Dict):
        """Print detailed statistics for a batch type."""
        print(f"\n{'='*70}")
        print(f"DETAILED STATISTICS: {results['batch_type']} Batch")
        print(f"{'='*70}\n")

        for batch_size, stats in results['batch_sizes'].items():
            print(f"Batch Size: {batch_size}")
            print(f"  Latency (ms):")
            print(f"    Min:    {stats['latency_ms']['min']:.2f}")
            print(f"    Max:    {stats['latency_ms']['max']:.2f}")
            print(f"    Mean:   {stats['latency_ms']['mean']:.2f}")
            print(f"    Median: {stats['latency_ms']['median']:.2f}")
            print(f"    StdDev: {stats['latency_ms']['stdev']:.2f}")
            print(f"    P95:    {stats['latency_ms']['p95']:.2f}")
            print(f"    P99:    {stats['latency_ms']['p99']:.2f}")
            print(f"  Throughput (ops/sec):")
            print(f"    Mean:   {stats['throughput_ops_sec']['mean']:.0f}")
            print(f"    Median: {stats['throughput_ops_sec']['median']:.0f}")
            print()

    async def cleanup(self):
        """Clean up test data."""
        print("Cleaning up test data...")

        # Delete benchmark data
        for batch_type in ['unlogged', 'logged']:
            items = await self.store.asearch(('benchmark', batch_type), limit=10000)

            for item in items:
                await self.store.adelete(item.namespace, item.key)

        print("✓ Cleanup complete\n")


async def main():
    """Run the benchmark suite."""
    # Configuration
    CONTACT_POINTS = ["127.0.0.1"]
    KEYSPACE = "advanced_features_store"
    BATCH_SIZES = [5, 10, 25, 50, 100]
    NUM_ITERATIONS = 10

    # Initialize benchmark
    benchmark = BatchBenchmark(CONTACT_POINTS, KEYSPACE)
    await benchmark.setup()

    # Benchmark UNLOGGED batches
    unlogged_results = await benchmark.benchmark_batch_type(
        batch_type="UNLOGGED",
        batch_sizes=BATCH_SIZES,
        num_iterations=NUM_ITERATIONS
    )

    # Benchmark LOGGED batches
    logged_results = await benchmark.benchmark_batch_type(
        batch_type="LOGGED",
        batch_sizes=BATCH_SIZES,
        num_iterations=NUM_ITERATIONS
    )

    # Print detailed statistics
    benchmark.print_detailed_stats(unlogged_results)
    benchmark.print_detailed_stats(logged_results)

    # Print comparison
    benchmark.print_comparison(unlogged_results, logged_results)

    # Recommendations
    print(f"\n{'='*70}")
    print("RECOMMENDATIONS")
    print(f"{'='*70}\n")

    print("UNLOGGED Batch:")
    print("  ✓ Use for high-throughput scenarios")
    print("  ✓ Best for non-critical data (logs, metrics, analytics)")
    print("  ✓ Atomic within single partition")
    print("  ✓ Significantly faster than LOGGED\n")

    print("LOGGED Batch:")
    print("  ✓ Use when full atomicity is required")
    print("  ✓ Best for critical transactions (financial, inventory)")
    print("  ✓ Atomic across all partitions")
    print("  ✓ Higher latency due to batch log writes\n")

    print("General Guidelines:")
    print("  • Keep batch sizes under 100 operations for best performance")
    print("  • Use UNLOGGED by default unless atomicity is critical")
    print("  • Monitor batch latency in production")
    print("  • Consider splitting large batches into smaller chunks\n")

    # Cleanup
    await benchmark.cleanup()

    print("✓ Benchmark complete!")


if __name__ == "__main__":
    asyncio.run(main())
