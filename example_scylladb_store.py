"""
Example usage of AsyncScyllaDBStore.

This script demonstrates how to use the ScyllaDB store with the same API
as LangGraph's AsyncPostgresStore.
"""

import asyncio
from datetime import datetime
from scylladb_store import AsyncScyllaDBStore, TTLConfig


async def main():
    """Demonstrate AsyncScyllaDBStore usage."""

    # Configuration
    contact_points = ["127.0.0.1"]
    keyspace = "langgraph_store"

    # TTL configuration (optional)
    ttl_config = TTLConfig(
        refresh_on_read=True,
        default_ttl=60.0,  # 60 minutes
        sweep_interval_minutes=5
    )

    # Create and initialize store
    async with AsyncScyllaDBStore.from_contact_points(
        contact_points=contact_points,
        keyspace=keyspace,
        ttl=ttl_config
    ) as store:
        # Setup database (creates tables if needed)
        await store.setup()

        print("=== Example 1: Basic CRUD Operations ===\n")

        # Put an item
        await store.aput(
            namespace=("users", "123"),
            key="profile",
            value={"name": "Alice", "email": "alice@example.com", "age": 30}
        )
        print("✓ Stored user profile")

        # Get the item
        item = await store.aget(("users", "123"), "profile")
        if item:
            print(f"✓ Retrieved: {item.value}")
            print(f"  Created at: {item.created_at}")
            print(f"  Namespace: {item.namespace}")

        # Update the item
        await store.aput(
            namespace=("users", "123"),
            key="profile",
            value={"name": "Alice", "email": "alice@example.com", "age": 31}
        )
        print("✓ Updated user profile")

        print("\n=== Example 2: Multiple Items in Same Namespace ===\n")

        # Add more items
        await store.aput(
            ("users", "123"),
            "preferences",
            {"theme": "dark", "notifications": True}
        )
        await store.aput(
            ("users", "123"),
            "settings",
            {"language": "en", "timezone": "UTC"}
        )
        print("✓ Stored preferences and settings")

        print("\n=== Example 3: Different Namespaces ===\n")

        # Store items in different namespaces
        await store.aput(
            ("users", "456"),
            "profile",
            {"name": "Bob", "email": "bob@example.com", "age": 25}
        )
        await store.aput(
            ("projects", "proj-1"),
            "metadata",
            {"title": "My Project", "status": "active", "score": 4.5}
        )
        await store.aput(
            ("projects", "proj-2"),
            "metadata",
            {"title": "Another Project", "status": "completed", "score": 5.0}
        )
        print("✓ Stored items in different namespaces")

        print("\n=== Example 4: Search with Filters ===\n")

        # Search in projects namespace
        results = await store.asearch(
            ("projects",),
            filter={"status": "active"},
            limit=10
        )
        print(f"Found {len(results)} active projects:")
        for item in results:
            print(f"  - {item.value.get('title')}: {item.value.get('status')}")

        # Search with comparison operators
        results = await store.asearch(
            ("projects",),
            filter={"score": {"$gte": 4.5}},
            limit=10
        )
        print(f"\nFound {len(results)} projects with score >= 4.5:")
        for item in results:
            print(f"  - {item.value.get('title')}: {item.value.get('score')}")

        print("\n=== Example 5: List Namespaces ===\n")

        # List all namespaces
        namespaces = await store.alist_namespaces()
        print("All namespaces:")
        for ns in namespaces:
            print(f"  - {ns}")

        # List namespaces with prefix
        user_namespaces = await store.alist_namespaces(prefix=("users",))
        print("\nUser namespaces:")
        for ns in user_namespaces:
            print(f"  - {ns}")

        # List namespaces with max_depth
        truncated = await store.alist_namespaces(max_depth=1)
        print("\nNamespaces truncated to depth 1:")
        for ns in truncated:
            print(f"  - {ns}")

        print("\n=== Example 6: Batch Operations ===\n")

        from scylladb_store import GetOp, PutOp, SearchOp, ListNamespacesOp

        # Batch multiple operations
        ops = [
            GetOp(namespace=("users", "123"), key="profile"),
            GetOp(namespace=("users", "456"), key="profile"),
            PutOp(
                namespace=("users", "789"),
                key="profile",
                value={"name": "Charlie", "email": "charlie@example.com"}
            ),
            SearchOp(
                namespace_prefix=("users",),
                filter=None,
                limit=10
            ),
            ListNamespacesOp(
                match_conditions=(("users",), None, None),
                limit=10
            )
        ]

        results = await store.abatch(ops)
        print(f"✓ Executed {len(ops)} operations in batch")
        print(f"  - Got user 123: {results[0].value.get('name') if results[0] else None}")
        print(f"  - Got user 456: {results[1].value.get('name') if results[1] else None}")
        print(f"  - Put user 789: Success")
        print(f"  - Search found {len(results[3])} users")
        print(f"  - Listed {len(results[4])} user namespaces")

        print("\n=== Example 7: TTL Support ===\n")

        # Store item with TTL (expires in 5 minutes)
        await store.aput(
            ("temp", "session-123"),
            "data",
            {"token": "abc123", "expires": "soon"},
            ttl=5.0  # 5 minutes
        )
        print("✓ Stored temporary item with 5 minute TTL")

        # Get item (refreshes TTL by default)
        temp_item = await store.aget(("temp", "session-123"), "data")
        print(f"✓ Retrieved temporary item: {temp_item.value}")

        # Get without refreshing TTL
        temp_item = await store.aget(
            ("temp", "session-123"),
            "data",
            refresh_ttl=False
        )
        print("✓ Retrieved without refreshing TTL")

        print("\n=== Example 8: Delete Operations ===\n")

        # Delete an item
        await store.adelete(("temp", "session-123"), "data")
        print("✓ Deleted temporary item")

        # Verify deletion
        deleted_item = await store.aget(("temp", "session-123"), "data")
        print(f"  Item exists after deletion: {deleted_item is not None}")

        print("\n=== Example 9: Synchronous API ===\n")

        # You can also use synchronous methods (not recommended in async context)
        # These are useful when calling from synchronous code
        # Note: Don't use sync methods from async code in production

        print("✓ Synchronous API is also available (get, put, delete, search, etc.)")

        print("\n" + "=" * 50)
        print("All examples completed successfully!")
        print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
