"""
Tests for production features (shutdown, health checks, timeouts, validation).
"""

import asyncio
import pytest
import logging
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from cassandra.cluster import Session

from vertector_scylladbstore import (
    AsyncScyllaDBStore,
    StoreValidationError,
    StoreTimeoutError,
)
from vertector_scylladbstore.logging_utils import (
    StructuredFormatter,
    PerformanceLogger,
    setup_production_logging,
)


# ============================================================================
# Graceful Shutdown Tests
# ============================================================================

class TestGracefulShutdown:
    """Test aclose() graceful shutdown behavior."""

    @pytest.mark.asyncio
    async def test_aclose_stops_ttl_sweeper(self, scylla_session, mock_embeddings):
        """Test that aclose() stops the TTL sweeper task."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_shutdown_ttl",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]},
            ttl={"enabled": True, "sweep_interval_seconds": 60}
        )
        await store.setup()

        # Verify TTL sweeper is running (or None if not enabled)
        if store._ttl_sweeper_task is not None:
            assert not store._ttl_sweeper_task.done()

            # Close store
            await store.aclose()

            # Verify TTL sweeper is stopped
            assert store._ttl_sweeper_task.done()
        else:
            # No TTL sweeper to stop
            await store.aclose()

    @pytest.mark.asyncio
    async def test_aclose_clears_caches(self, scylla_session, mock_embeddings):
        """Test that aclose() clears prepared statement and embedding caches."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_caches",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Populate caches
        store._prepared_statements["test"] = Mock()
        store._embedding_cache.put("test_key", [0.1, 0.2, 0.3])

        assert len(store._prepared_statements) > 0
        assert store._embedding_cache.get("test_key") is not None

        # Close store
        await store.aclose()

        # Verify caches are cleared
        assert len(store._prepared_statements) == 0
        assert store._embedding_cache.get("test_key") is None

    @pytest.mark.asyncio
    async def test_aclose_closes_qdrant_client(self, scylla_session, mock_embeddings):
        """Test that aclose() closes Qdrant client connection."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_qdrant_close",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Mock Qdrant client close method
        store.qdrant_client.close = AsyncMock()

        # Close store
        await store.aclose()

        # Verify Qdrant close was called
        store.qdrant_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_aclose_handles_timeout_gracefully(self, scylla_session, mock_embeddings):
        """Test that aclose() handles TTL sweeper timeout gracefully."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_timeout_handle",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]},
            ttl={"enabled": True, "sweep_interval_seconds": 60}
        )
        await store.setup()

        # Only test if TTL sweeper exists
        if store._ttl_sweeper_task is not None:
            # Make TTL sweeper hang (won't respond to stop event)
            async def hanging_sweeper():
                while True:
                    await asyncio.sleep(1)

            store._ttl_sweeper_task.cancel()
            store._ttl_sweeper_task = asyncio.create_task(hanging_sweeper())

            # Should not raise, should handle timeout
            await store.aclose()

            # Task should be cancelled
            assert store._ttl_sweeper_task.cancelled() or store._ttl_sweeper_task.done()
        else:
            # No TTL sweeper, just test cleanup works
            await store.aclose()


# ============================================================================
# Health Check Tests
# ============================================================================

class TestHealthChecks:
    """Test health_check() scenarios."""

    @pytest.mark.asyncio
    async def test_health_check_all_healthy(self, scylla_session, mock_embeddings):
        """Test health check when all services are healthy."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_health",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        health = await store.health_check()

        assert health["overall"] == "healthy"
        assert health["scylladb"]["status"] == "healthy"
        assert "latency_ms" in health["scylladb"]
        assert health["qdrant"]["status"] == "healthy"
        assert "latency_ms" in health["qdrant"]

    @pytest.mark.asyncio
    async def test_health_check_scylladb_unhealthy(self, scylla_session, mock_embeddings):
        """Test health check when ScyllaDB is unhealthy."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_scylla_down",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Mock ScyllaDB failure
        async def failing_execute(*args, **kwargs):
            raise Exception("Connection refused")

        store._execute_async = failing_execute

        health = await store.health_check()

        assert health["overall"] == "unhealthy"
        assert health["scylladb"]["status"] == "unhealthy"
        assert "error" in health["scylladb"]

    @pytest.mark.asyncio
    async def test_health_check_qdrant_unhealthy(self, scylla_session, mock_embeddings):
        """Test health check when Qdrant is unhealthy."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_qdrant_down",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]}
        )
        await store.setup()

        # Mock Qdrant failure
        async def failing_get_collections():
            raise Exception("Qdrant unavailable")

        store.qdrant_client.get_collections = failing_get_collections

        health = await store.health_check()

        assert health["overall"] == "unhealthy"
        assert health["qdrant"]["status"] == "unhealthy"
        assert "error" in health["qdrant"]


# ============================================================================
# Timeout Tests
# ============================================================================

class TestTimeoutEnforcement:
    """Test timeout configurations and enforcement."""

    @pytest.mark.asyncio
    async def test_qdrant_timeout_enforced(self, scylla_session, mock_embeddings):
        """Test that Qdrant operations timeout correctly."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_timeout",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]},
            qdrant_timeout=0.1  # 100ms timeout
        )
        await store.setup()

        # Create slow Qdrant operation
        async def slow_operation():
            await asyncio.sleep(1)  # 1 second (exceeds timeout)
            return []

        # Should raise StoreTimeoutError
        with pytest.raises(StoreTimeoutError) as exc_info:
            await store._with_qdrant_timeout(slow_operation(), operation="test")

        assert "timed out" in str(exc_info.value).lower()
        assert exc_info.value.timeout_seconds == 0.1

    @pytest.mark.asyncio
    async def test_qdrant_timeout_success_within_limit(self, scylla_session, mock_embeddings):
        """Test that Qdrant operations succeed within timeout."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_success",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]},
            qdrant_timeout=1.0  # 1 second timeout
        )
        await store.setup()

        # Create fast Qdrant operation
        async def fast_operation():
            await asyncio.sleep(0.01)  # 10ms (well within timeout)
            return {"result": "success"}

        result = await store._with_qdrant_timeout(fast_operation(), operation="test")
        assert result == {"result": "success"}

    @pytest.mark.asyncio
    async def test_custom_timeout_configuration(self, scylla_session, mock_embeddings):
        """Test custom timeout values are stored correctly."""
        store = AsyncScyllaDBStore(
            session=scylla_session,
            keyspace="test_custom",
            index={"dims": 768, "embed": mock_embeddings, "fields": ["$"]},
            query_timeout=30.0,
            qdrant_timeout=15.0
        )

        assert store.query_timeout == 30.0
        assert store.qdrant_timeout == 15.0


# ============================================================================
# Keyspace Validation Tests
# ============================================================================

class TestKeyspaceValidation:
    """Test CQL injection prevention via keyspace validation."""

    def test_valid_keyspace_names(self, scylla_session):
        """Test that valid keyspace names are accepted."""
        valid_names = [
            "test_store",
            "production",
            "my_keyspace_123",
            "a",
            "Z",
            "CamelCase123",
        ]

        for name in valid_names:
            # Should not raise
            store = AsyncScyllaDBStore(
                session=scylla_session,
                keyspace=name
            )
            assert store.keyspace == name

    def test_invalid_keyspace_empty(self, scylla_session):
        """Test that empty keyspace name is rejected."""
        with pytest.raises(StoreValidationError) as exc_info:
            AsyncScyllaDBStore(
                session=scylla_session,
                keyspace=""
            )
        assert "cannot be empty" in str(exc_info.value).lower()

    def test_invalid_keyspace_special_chars(self, scylla_session):
        """Test that keyspace names with special characters are rejected."""
        invalid_names = [
            "test-store",      # hyphen
            "test.store",      # dot
            "test store",      # space
            "test;drop",       # semicolon (SQL injection attempt)
            "test'or'1=1",     # quote (SQL injection attempt)
            "123test",         # starts with number
            "_test",           # starts with underscore
        ]

        for name in invalid_names:
            with pytest.raises(StoreValidationError):
                AsyncScyllaDBStore(
                    session=scylla_session,
                    keyspace=name
                )

    def test_invalid_keyspace_reserved_keywords(self, scylla_session):
        """Test that reserved CQL keywords are rejected."""
        reserved = [
            "select", "SELECT",
            "insert", "INSERT",
            "update", "UPDATE",
            "delete", "DELETE",
            "drop", "DROP",
            "table", "TABLE",
            "keyspace", "KEYSPACE",
        ]

        for keyword in reserved:
            with pytest.raises(StoreValidationError) as exc_info:
                AsyncScyllaDBStore(
                    session=scylla_session,
                    keyspace=keyword
                )
            assert "reserved" in str(exc_info.value).lower()

    def test_invalid_keyspace_too_long(self, scylla_session):
        """Test that keyspace names exceeding 48 chars are rejected."""
        long_name = "a" * 49  # 49 characters (exceeds 48 limit)

        with pytest.raises(StoreValidationError) as exc_info:
            AsyncScyllaDBStore(
                session=scylla_session,
                keyspace=long_name
            )
        assert "exceeds maximum length" in str(exc_info.value).lower()

    def test_invalid_keyspace_wrong_type(self, scylla_session):
        """Test that non-string keyspace names are rejected."""
        with pytest.raises(StoreValidationError) as exc_info:
            AsyncScyllaDBStore(
                session=scylla_session,
                keyspace=123  # integer instead of string
            )
        assert "must be a string" in str(exc_info.value).lower()


# ============================================================================
# Structured Logging Tests
# ============================================================================

class TestStructuredLogging:
    """Test structured logging utilities."""

    def test_structured_formatter_json_output(self):
        """Test that StructuredFormatter outputs valid JSON."""
        import json

        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Test message",
            args=(),
            exc_info=None
        )

        output = formatter.format(record)
        data = json.loads(output)  # Should not raise

        assert data["level"] == "INFO"
        assert data["message"] == "Test message"
        assert data["logger"] == "test_logger"
        assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_performance_logger_success(self, caplog):
        """Test PerformanceLogger logs duration on success."""
        logger = logging.getLogger("test_perf")
        logger.setLevel(logging.INFO)

        async with PerformanceLogger("test_op", logger=logger, context="value"):
            await asyncio.sleep(0.01)

        # Check logs contain operation start and completion
        assert any("Starting operation: test_op" in record.message for record in caplog.records)
        assert any("Operation completed: test_op" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_performance_logger_failure(self, caplog):
        """Test PerformanceLogger logs errors on failure."""
        logger = logging.getLogger("test_perf_fail")
        logger.setLevel(logging.ERROR)

        with pytest.raises(ValueError):
            async with PerformanceLogger("failing_op", logger=logger):
                raise ValueError("Test error")

        # Check logs contain failure message
        assert any("Operation failed: failing_op" in record.message for record in caplog.records)

    def test_setup_production_logging_json(self):
        """Test production logging setup with JSON format."""
        setup_production_logging(level="INFO", format="json")

        root_logger = logging.getLogger()
        assert root_logger.level == logging.INFO
        assert len(root_logger.handlers) > 0
        assert isinstance(root_logger.handlers[0].formatter, StructuredFormatter)

    def test_setup_production_logging_text(self):
        """Test production logging setup with text format."""
        setup_production_logging(level="DEBUG", format="text")

        root_logger = logging.getLogger()
        assert root_logger.level == logging.DEBUG
        assert len(root_logger.handlers) > 0
        assert not isinstance(root_logger.handlers[0].formatter, StructuredFormatter)
