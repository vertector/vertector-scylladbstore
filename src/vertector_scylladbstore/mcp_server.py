
import asyncio
import json
import logging
import os
import sys
import ast
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    ImageContent,
    EmbeddedResource,
    Resource,
    ResourceContents,
    TextResourceContents
)

from vertector_scylladbstore.store import AsyncScyllaDBStore
from vertector_scylladbstore.config import load_config_from_env, load_index_config_from_env

# Configure logging to stderr so it doesn't interfere with stdio communication
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("vertector_mcp")

# Initialize Server
server = Server("vertector-scylladbstore")

# Global store instance
_store: AsyncScyllaDBStore | None = None


@asynccontextmanager
async def _get_store() -> AsyncIterator[AsyncScyllaDBStore]:
    """
    Context manager to get or create the store instance.
    """
    global _store
    if _store is None:
        try:
             # Basic setup without specific embedding model for generic usage
             load_config_from_env() # Just to ensure env vars are processed if needed
             
             contact_points = os.getenv("SCYLLADB_CONTACT_POINTS", "127.0.0.1").split(",")
             keyspace = os.getenv("SCYLLADB_KEYSPACE", "vertector_store")
             qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
             
             index_config = None
             try:
                 index_config = load_index_config_from_env()
             except Exception:
                 logger.warning("Could not load index config from env. Semantic search may be limited.")

             _store = await AsyncScyllaDBStore.create(
                 contact_points=contact_points,
                 keyspace=keyspace,
                 qdrant_url=qdrant_url,
                 index=index_config,
                 enable_tracing=True,
             )
             await _store.setup()
        except Exception as e:
            logger.error(f"Failed to initialize store: {e}")
            raise

    yield _store


async def close_store():
    """Cleanup store resources."""
    global _store
    if _store:
        await _store.aclose()
        _store = None


@server.list_resources()
async def list_resources() -> List[Resource]:
    return [
        Resource(
            uri="store://metrics",
            name="Store Metrics",
            description="Current performance metrics of the ScyllaDB/Qdrant store",
            mimeType="application/json"
        )
        # Note: We cannot easily list all resources (documents) here as there could be millions.
        # We rely on specific read_resource calls.
    ]


@server.read_resource()
async def read_resource(uri: str) -> str | bytes | ResourceContents: # type hints vary in SDK
    if uri == "store://metrics":
        async with _get_store() as store:
            stats = store.metrics.get_stats()
            return TextResourceContents(
                uri=uri,
                mimeType="application/json",
                text=json.dumps(stats, indent=2)
            )
    
    if uri.startswith("store://"):
        # Format: store://namespace/key
        # Example: store://users/123/profile
        try:
            path = uri.replace("store://", "")
            parts = path.split("/")
            if len(parts) < 2:
                raise ValueError("Invalid URI format. Expected store://namespace/key")
            
            key = parts[-1]
            namespace = tuple(parts[:-1])
            
            async with _get_store() as store:
                item = await store.aget(namespace=namespace, key=key)
                if item:
                    return TextResourceContents(
                        uri=uri,
                        mimeType="application/json",
                        text=json.dumps({
                            "value": item.value,
                            "created_at": str(item.created_at),
                            "updated_at": str(item.updated_at)
                        }, indent=2)
                    )
                else:
                    raise ValueError(f"Item not found: {uri}")
        except Exception as e:
            # mcp framework captures exceptions, but good to be explicit
            raise ValueError(f"Failed to read resource {uri}: {e}")

    raise ValueError(f"Unknown resource: {uri}")


@server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="search_store",
            description="Search the knowledge base using semantic search (vector similarity) or filter search.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query"
                    },
                    "namespace_prefix": {
                        "type": "tuple",
                        "items": {"type": "string"},
                        "description": "Optional namespace tuple to restrict search (e.g., ('users', '123'))"
                    },
                    "filter": {
                        "type": "object",
                        "description": "Optional metadata filters (e.g., {'category': 'AI'})"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="save_memory",
            description="Save or create a new memory/document in the store.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "object"},
                    "namespace": {
                        "type": "tuple", 
                        "items": {"type": "string"},
                        "description": "Namespace tuple (e.g., ('users', '123'))"
                    },
                    "ttl": {"type": "number", "description": "TTL in seconds"},
                    "wait_sync": {"type": "boolean", "default": True}
                },
                "required": ["key", "value", "namespace"]
            }
        ),
         Tool(
            name="update_memory",
            description="Update an existing memory/document store.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "object"},
                    "namespace": {
                        "type": "tuple",
                        "items": {"type": "string"},
                        "description": "Namespace tuple (e.g., ('users', '123'))"
                    },
                    "ttl": {"type": "number"}
                },
                "required": ["key", "value", "namespace"]
            }
        ),
        Tool(
            name="read_memory",
            description="Retrieve a specific item from the store.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "namespace": {"type": "string"}
                },
                "required": ["key", "namespace"]
            }
        ),
        Tool(
            name="delete_memory",
            description="Delete an item from the store.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "namespace": {"type": "string"}
                },
                "required": ["key", "namespace"]
            }
        ),
        Tool(
            name="list_namespaces",
            description="List all namespace paths available in the store.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "tuple",
                        "items": {"type": "string"},
                        "description": "Optional prefix tuple filter (e.g., ('users',))"
                    }
                }
            }
        )
    ]


def _ensure_tuple(val: Any) -> tuple[str, ...]:
    """
    Ensure the namespace value is converted to a tuple of strings.
    Strictly accepts stringified tuples or Python tuples (internal).
    Rejects lists to enforce 'tuple' semantics.
    """
    if isinstance(val, list):
         raise ValueError("Namespace must be a tuple, not a list.")

    if isinstance(val, tuple):
        return tuple(str(x) for x in val)
    
    if isinstance(val, str):
        # clean up potential wrapping quotes if the LLM double-encoded it
        val = val.strip()
        if val.startswith("(") and val.endswith(")"):
            try:
                # Safely evaluate string representation of tuple
                parsed = ast.literal_eval(val)
                if isinstance(parsed, tuple):
                    return tuple(str(x) for x in parsed)
            except Exception:
                pass
             
    # If we get here, it might be a single string acting as a 1-element tuple, 
    # OR we failed to parse a tuple string.
    # To be strictly 'tuple not list', we should probably try to respect the string if it wasn't a list.
    return (str(val),)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    async with _get_store() as store:
        try:
            if name == "search_store":
                query = arguments["query"]
                namespace_prefix = arguments.get("namespace_prefix")
                filter_dict = arguments.get("filter")
                limit = arguments.get("limit", 5)
                
                ns_tuple = _ensure_tuple(namespace_prefix) if namespace_prefix else ()
                
                results = await store.asearch(
                    namespace_prefix=ns_tuple,
                    query=query,
                    filter=filter_dict,
                    limit=limit
                )
                
                formatted = []
                for item in results:
                    formatted.append({
                        "score": round(item.score, 4),
                        "namespace": item.namespace,  # Keep as list/tuple
                        "key": item.key,
                        "value": item.value
                    })
                return [TextContent(type="text", text=json.dumps(formatted, indent=2))]

            elif name == "save_memory" or name == "update_memory":
                # Both use aput
                key = arguments["key"]
                value = arguments["value"]
                namespace = arguments["namespace"]
                ttl = arguments.get("ttl")
                wait_sync = arguments.get("wait_sync", True)
                
                ns_tuple = _ensure_tuple(namespace)
                
                await store.aput(
                    namespace=ns_tuple,
                    key=key,
                    value=value,
                    ttl=ttl,
                    wait_for_vector_sync=wait_sync
                )
                return [TextContent(type="text", text=f"Successfully saved item at {namespace}/{key}")]

            elif name == "read_memory":
                key = arguments["key"]
                namespace = arguments["namespace"]
                ns_tuple = _ensure_tuple(namespace)
                
                item = await store.aget(namespace=ns_tuple, key=key)
                if item:
                    data = {
                        "namespace": item.namespace, # JSON serialization handles list/tuple
                        "key": item.key,
                        "value": item.value,
                        "created_at": str(item.created_at),
                        "updated_at": str(item.updated_at)
                    }
                    return [TextContent(type="text", text=json.dumps(data, indent=2))]
                else:
                    return [TextContent(type="text", text="Item not found")]

            elif name == "delete_memory":
                key = arguments["key"]
                namespace = arguments["namespace"]
                ns_tuple = _ensure_tuple(namespace)
                
                await store.adelete(namespace=ns_tuple, key=key)
                return [TextContent(type="text", text=f"Successfully deleted item at {namespace}/{key}")]

            elif name == "list_namespaces":
                prefix = arguments.get("prefix")
                prefix_tuple = _ensure_tuple(prefix) if prefix else None
                
                namespaces = await store.alist_namespaces(prefix=prefix_tuple)
                # Return list of lists (tuples), not slash strings, to be consistent
                return [TextContent(type="text", text=json.dumps(namespaces, indent=2))]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        except Exception as e:
            logger.error(f"Error executing tool {name}: {e}", exc_info=True)
            return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    logger.info("Starting Vertector ScyllaDB MCP Server")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
    finally:
        await close_store()

if __name__ == "__main__":
    asyncio.run(main())
