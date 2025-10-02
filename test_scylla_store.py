"""
Quick test of AsyncScyllaDBStore implementation.
"""

import asyncio
import sys
from scylladb_store import AsyncScyllaDBStore, TTLConfig


async def main():
    """Test AsyncScyllaDBStore."""

    print("=" * 60)
    print("Testing AsyncScyllaDBStore Implementation")
    print("=" * 60)

    # Connect to ScyllaDB
    contact_points = ["127.0.0.1"]
    keyspace = "test_langgraph_store"

    ttl_config = TTLConfig(
        refresh_on_read=True,
        default_ttl=None
    )

    try:
        async with AsyncScyllaDBStore.from_contact_points(
            contact_points=contact_points,
            keyspace=keyspace,
            ttl=ttl_config
        ) as store:
            print("\n✓ Connected to ScyllaDB")

            # Setup database
            await store.setup()
            print("✓ Database setup complete")

            # Test 1: Basic put/get
            print("\n--- Test 1: Basic PUT and GET ---")
            await store.aput(
                namespace=("users", "123"),
                key="profile",
                value={"name": "Alice", "age": 30}
            )
            print("✓ PUT: stored user profile")

            item = await store.aget(("users", "123"), "profile")
            assert item is not None
            assert item.value["name"] == "Alice"
            assert item.value["age"] == 30
            print(f"✓ GET: retrieved {item.value}")

            # Test 2: Update
            print("\n--- Test 2: UPDATE ---")
            await store.aput(
                namespace=("users", "123"),
                key="profile",
                value={"name": "Alice", "age": 31}
            )
            item = await store.aget(("users", "123"), "profile")
            assert item.value["age"] == 31
            print(f"✓ UPDATE: age updated to {item.value['age']}")

            # Test 3: Multiple items
            print("\n--- Test 3: Multiple Items ---")
            await store.aput(("users", "456"), "profile", {"name": "Bob", "age": 25})
            await store.aput(("projects", "p1"), "info", {"title": "Project 1", "status": "active"})
            await store.aput(("projects", "p2"), "info", {"title": "Project 2", "status": "completed"})
            print("✓ Stored multiple items in different namespaces")

            # Test 4: Search
            print("\n--- Test 4: SEARCH ---")
            results = await store.asearch(("projects",), limit=10)
            print(f"✓ Found {len(results)} projects")
            for r in results:
                print(f"  - {r.value['title']}: {r.value['status']}")

            # Test 5: Search with filter
            print("\n--- Test 5: SEARCH with Filter ---")
            results = await store.asearch(
                ("projects",),
                filter={"status": "active"},
                limit=10
            )
            print(f"✓ Found {len(results)} active projects")
            assert len(results) == 1
            assert results[0].value["title"] == "Project 1"

            # Test 6: List namespaces
            print("\n--- Test 6: LIST NAMESPACES ---")
            namespaces = await store.alist_namespaces()
            print(f"✓ Found {len(namespaces)} namespaces:")
            for ns in namespaces:
                print(f"  - {ns}")

            # Test 7: Delete
            print("\n--- Test 7: DELETE ---")
            await store.adelete(("users", "456"), "profile")
            deleted = await store.aget(("users", "456"), "profile")
            assert deleted is None
            print("✓ Deleted item successfully")

            # Test 8: Batch operations
            print("\n--- Test 8: BATCH Operations ---")
            from scylladb_store import GetOp, PutOp, SearchOp

            ops = [
                GetOp(namespace=("users", "123"), key="profile"),
                PutOp(
                    namespace=("users", "789"),
                    key="profile",
                    value={"name": "Charlie", "age": 35}
                ),
                SearchOp(
                    namespace_prefix=("users",),
                    limit=10
                )
            ]

            results = await store.abatch(ops)
            print(f"✓ Executed {len(ops)} operations in batch")
            print(f"  - GET result: {results[0].value['name']}")
            print(f"  - PUT result: Success")
            print(f"  - SEARCH result: {len(results[2])} users found")

            # Test 9: TTL
            print("\n--- Test 9: TTL Support ---")
            await store.aput(
                ("temp", "session-1"),
                "data",
                {"token": "abc123"},
                ttl=1.0  # 1 minute
            )
            print("✓ Stored item with 1 minute TTL")

            temp = await store.aget(("temp", "session-1"), "data")
            assert temp is not None
            print(f"✓ Retrieved temporary item: {temp.value}")

            print("\n" + "=" * 60)
            print("ALL TESTS PASSED! ✨")
            print("=" * 60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
