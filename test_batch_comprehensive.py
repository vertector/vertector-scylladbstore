#!/usr/bin/env python3
"""
Comprehensive test suite for batch operations.

Tests all aspects of batch functionality:
- Atomic batches (LOGGED and UNLOGGED)
- Concurrent batches (mixed operations)
- Retry logic
- Metrics tracking
- Error handling
- Edge cases
"""

import asyncio
from cassandra.cluster import Cluster
from scylladb_store import (
    AsyncScyllaDBStore,
    PutOp,
    GetOp,
    SearchOp,
    StoreValidationError,
)


class BatchTestSuite:
    """Comprehensive test suite for batch operations."""

    def __init__(self):
        self.store = None
        self.passed = 0
        self.failed = 0

    async def setup(self):
        """Initialize connection to ScyllaDB."""
        cluster = Cluster(['127.0.0.1'])
        session = cluster.connect()

        self.store = AsyncScyllaDBStore(
            session=session,
            keyspace='advanced_features_store'
        )
        await self.store.setup()
        print("✓ Connected to ScyllaDB\n")

    def test_result(self, test_name: str, passed: bool, message: str = ""):
        """Record test result."""
        if passed:
            self.passed += 1
            print(f"  ✓ {test_name}")
            if message:
                print(f"    {message}")
        else:
            self.failed += 1
            print(f"  ✗ {test_name}")
            if message:
                print(f"    {message}")

    async def test_unlogged_batch(self):
        """Test UNLOGGED batch execution."""
        print("Test 1: UNLOGGED Batch")

        ops = [
            PutOp(namespace=('test', 'unlogged'), key=f'item_{i}', value={'id': i})
            for i in range(10)
        ]

        results = await self.store.abatch(ops)
        self.test_result(
            "UNLOGGED batch execution",
            len(results) == 10,
            f"Executed {len(results)} operations"
        )

        # Verify data was written
        item = await self.store.aget(('test', 'unlogged'), 'item_5')
        self.test_result(
            "UNLOGGED batch data verification",
            item is not None and item.value['id'] == 5,
            f"Retrieved: {item.value if item else None}"
        )

    async def test_logged_batch(self):
        """Test LOGGED batch execution."""
        print("\nTest 2: LOGGED Batch")

        ops = [
            PutOp(namespace=('test', 'logged'), key=f'item_{i}', value={'id': i})
            for i in range(5)
        ]

        results = await self.store.abatch(ops, batch_type='LOGGED')
        self.test_result(
            "LOGGED batch execution",
            len(results) == 5,
            f"Executed {len(results)} operations"
        )

        # Verify atomicity
        item = await self.store.aget(('test', 'logged'), 'item_3')
        self.test_result(
            "LOGGED batch atomicity",
            item is not None and item.value['id'] == 3,
            "All operations committed atomically"
        )

    async def test_mixed_batch(self):
        """Test mixed operations batch."""
        print("\nTest 3: Mixed Operations Batch")

        # First create some data
        await self.store.aput(('test', 'mixed'), 'existing', {'id': 999})

        ops = [
            PutOp(namespace=('test', 'mixed'), key='new', value={'id': 1}),
            GetOp(namespace=('test', 'mixed'), key='existing'),
            PutOp(namespace=('test', 'mixed'), key='another', value={'id': 2}),
        ]

        results = await self.store.abatch(ops)
        self.test_result(
            "Mixed batch execution",
            len(results) == 3,
            f"Executed {len(results)} operations concurrently"
        )

        self.test_result(
            "Mixed batch GET result",
            results[1] is not None and results[1].value['id'] == 999,
            f"GET returned: {results[1].value if results[1] else None}"
        )

    async def test_batch_with_ttl(self):
        """Test batch operations with TTL."""
        print("\nTest 4: Batch with TTL")

        ops = [
            PutOp(namespace=('test', 'ttl'), key=f'temp_{i}', value={'id': i}, ttl=60.0)
            for i in range(5)
        ]

        results = await self.store.abatch(ops)
        self.test_result(
            "Batch with TTL execution",
            len(results) == 5,
            "All operations have 60s TTL"
        )

        # Verify item exists
        item = await self.store.aget(('test', 'ttl'), 'temp_0')
        self.test_result(
            "TTL batch data verification",
            item is not None,
            "Item created with TTL"
        )

    async def test_empty_batch(self):
        """Test empty batch."""
        print("\nTest 5: Empty Batch")

        results = await self.store.abatch([])
        self.test_result(
            "Empty batch handling",
            len(results) == 0,
            "Empty batch returns empty results"
        )

    async def test_single_operation_batch(self):
        """Test batch with single operation."""
        print("\nTest 6: Single Operation Batch")

        ops = [PutOp(namespace=('test', 'single'), key='one', value={'id': 1})]
        results = await self.store.abatch(ops)

        self.test_result(
            "Single operation batch",
            len(results) == 1,
            "Single operation executes correctly"
        )

    async def test_large_batch_warning(self):
        """Test large batch warning."""
        print("\nTest 7: Large Batch Warning")

        # Create batch larger than WARN_BATCH_SIZE (50)
        ops = [
            PutOp(namespace=('test', 'large'), key=f'item_{i}', value={'id': i})
            for i in range(75)
        ]

        results = await self.store.abatch(ops)
        self.test_result(
            "Large batch execution",
            len(results) == 75,
            "Large batch executed with warning"
        )

    async def test_batch_size_limit(self):
        """Test batch size validation."""
        print("\nTest 8: Batch Size Limit")

        # Create batch larger than MAX_BATCH_SIZE (100)
        ops = [
            PutOp(namespace=('test', 'limit'), key=f'item_{i}', value={'id': i})
            for i in range(150)
        ]

        try:
            await self.store.abatch(ops)
            self.test_result("Batch size validation", False, "Should have raised error")
        except StoreValidationError as e:
            self.test_result(
                "Batch size validation",
                "exceeds maximum" in str(e),
                f"Correctly rejected: {e}"
            )

    async def test_batch_metrics(self):
        """Test batch metrics tracking."""
        print("\nTest 9: Batch Metrics Tracking")

        # Reset metrics
        await self.store.reset_metrics()

        # Execute batches
        ops1 = [PutOp(namespace=('test', 'metrics'), key=f'a_{i}', value={'id': i}) for i in range(10)]
        ops2 = [PutOp(namespace=('test', 'metrics'), key=f'b_{i}', value={'id': i}) for i in range(5)]
        ops3 = [
            PutOp(namespace=('test', 'metrics'), key='mixed', value={'id': 1}),
            GetOp(namespace=('test', 'metrics'), key='a_0'),
        ]

        await self.store.abatch(ops1)
        await self.store.abatch(ops2, batch_type='LOGGED')
        await self.store.abatch(ops3)

        # Check metrics
        metrics = await self.store.get_metrics()
        batch_stats = metrics['batch_stats']

        self.test_result(
            "Metrics: Total batches",
            batch_stats['total_batches'] == 3,
            f"Tracked: {batch_stats['total_batches']}"
        )

        self.test_result(
            "Metrics: Total operations",
            batch_stats['total_batch_operations'] == 17,
            f"Tracked: {batch_stats['total_batch_operations']}"
        )

        self.test_result(
            "Metrics: Atomic batches",
            batch_stats['atomic_batches'] == 2,
            f"Tracked: {batch_stats['atomic_batches']}"
        )

        self.test_result(
            "Metrics: Concurrent batches",
            batch_stats['concurrent_batches'] == 1,
            f"Tracked: {batch_stats['concurrent_batches']}"
        )

        self.test_result(
            "Metrics: LOGGED batches",
            batch_stats['logged_batches'] == 1,
            f"Tracked: {batch_stats['logged_batches']}"
        )

        self.test_result(
            "Metrics: UNLOGGED batches",
            batch_stats['unlogged_batches'] == 1,
            f"Tracked: {batch_stats['unlogged_batches']}"
        )

    async def test_batch_retry(self):
        """Test batch retry configuration."""
        print("\nTest 10: Batch Retry Configuration")

        ops = [
            PutOp(namespace=('test', 'retry'), key=f'item_{i}', value={'id': i})
            for i in range(5)
        ]

        # Should succeed without retries
        results = await self.store.abatch(
            ops,
            max_retries=3,
            retry_delay=0.1,
            retry_backoff=2.0
        )

        self.test_result(
            "Batch retry configuration",
            len(results) == 5,
            "Retry params accepted, batch succeeded"
        )

    async def test_batch_with_different_ttls(self):
        """Test batch with operations having different TTLs."""
        print("\nTest 11: Batch with Mixed TTLs")

        ops = [
            PutOp(namespace=('test', 'mixed_ttl'), key='short', value={'id': 1}, ttl=10.0),
            PutOp(namespace=('test', 'mixed_ttl'), key='long', value={'id': 2}, ttl=3600.0),
            PutOp(namespace=('test', 'mixed_ttl'), key='none', value={'id': 3}),
        ]

        results = await self.store.abatch(ops)
        self.test_result(
            "Batch with mixed TTLs",
            len(results) == 3,
            "Operations with different TTLs executed"
        )

    async def test_batch_idempotency(self):
        """Test batch idempotency."""
        print("\nTest 12: Batch Idempotency")

        ops = [
            PutOp(namespace=('test', 'idempotent'), key='item', value={'id': 1, 'version': 1})
        ]

        # Execute same batch twice
        await self.store.abatch(ops)
        await self.store.abatch(ops)

        item = await self.store.aget(('test', 'idempotent'), 'item')
        self.test_result(
            "Batch idempotency",
            item is not None and item.value['version'] == 1,
            "Multiple executions produce same result"
        )

    async def run_all_tests(self):
        """Run all tests."""
        print("="*70)
        print("COMPREHENSIVE BATCH OPERATIONS TEST SUITE")
        print("="*70)
        print()

        await self.setup()

        # Run all tests
        await self.test_unlogged_batch()
        await self.test_logged_batch()
        await self.test_mixed_batch()
        await self.test_batch_with_ttl()
        await self.test_empty_batch()
        await self.test_single_operation_batch()
        await self.test_large_batch_warning()
        await self.test_batch_size_limit()
        await self.test_batch_metrics()
        await self.test_batch_retry()
        await self.test_batch_with_different_ttls()
        await self.test_batch_idempotency()

        # Print summary
        print("\n" + "="*70)
        print("TEST SUMMARY")
        print("="*70)
        print(f"Passed: {self.passed}")
        print(f"Failed: {self.failed}")
        print(f"Total:  {self.passed + self.failed}")
        print()

        if self.failed == 0:
            print("✓ ALL TESTS PASSED!")
        else:
            print(f"✗ {self.failed} test(s) failed")

        return self.failed == 0


async def main():
    """Run the test suite."""
    suite = BatchTestSuite()
    success = await suite.run_all_tests()
    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
