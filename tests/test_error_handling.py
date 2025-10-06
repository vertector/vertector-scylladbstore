"""
Tests for error handling and resilience.

Tests:
- Circuit breaker behavior
- Retry logic
- Qdrant sync failures
- Invalid input validation
- Connection errors
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.unit
class TestInputValidation:
    """Test input validation for operations."""

    @pytest.mark.asyncio
    async def test_put_invalid_namespace(self, store_no_embeddings):
        """Test PUT with invalid namespace type."""
        from vertector_scylladbstore import StoreValidationError

        with pytest.raises((StoreValidationError, TypeError, ValueError)):
            await store_no_embeddings.aput(
                namespace="invalid",  # Should be tuple
                key="test",
                value={"data": "test"}
            )

    @pytest.mark.asyncio
    async def test_put_empty_key(self, store_no_embeddings):
        """Test PUT with empty key."""
        from vertector_scylladbstore import StoreValidationError

        with pytest.raises((StoreValidationError, ValueError)):
            await store_no_embeddings.aput(
                namespace=("test",),
                key="",  # Empty key
                value={"data": "test"}
            )

    @pytest.mark.asyncio
    async def test_put_invalid_value_type(self, store_no_embeddings):
        """Test PUT with non-dict value."""
        from vertector_scylladbstore import StoreValidationError

        with pytest.raises((StoreValidationError, TypeError, ValueError)):
            await store_no_embeddings.aput(
                namespace=("test",),
                key="test",
                value="not a dict"  # Should be dict
            )

    @pytest.mark.asyncio
    async def test_search_invalid_limit(self, store_no_embeddings):
        """Test SEARCH with negative limit."""
        # Negative limit should be handled gracefully (return empty or error)
        results = await store_no_embeddings.asearch(
            ("test",),
            limit=-1
        )
        # Should either raise error or return empty
        assert isinstance(results, list)


@pytest.mark.integration
class TestQdrantSyncFailures:
    """Test Qdrant sync failure handling."""

    @pytest.mark.asyncio
    async def test_qdrant_sync_failure_raises_error(self, store_with_mock_embeddings):
        """Test that Qdrant sync failures raise errors (not silent)."""
        from vertector_scylladbstore import StoreQueryError

        # Mock Qdrant client to always fail
        original_upsert = store_with_mock_embeddings.qdrant_client.upsert

        async def failing_upsert(*args, **kwargs):
            raise Exception("Qdrant connection error")

        store_with_mock_embeddings.qdrant_client.upsert = failing_upsert

        # Attempt PUT with wait_for_vector_sync=True
        with pytest.raises(StoreQueryError) as exc_info:
            await store_with_mock_embeddings.aput(
                ("test",),
                "item",
                {"content": "test data"},
                wait_for_vector_sync=True
            )

        # Verify error message mentions data inconsistency
        assert "inconsistency" in str(exc_info.value).lower() or "failed" in str(exc_info.value).lower()

        # Restore original
        store_with_mock_embeddings.qdrant_client.upsert = original_upsert

    @pytest.mark.asyncio
    async def test_qdrant_eventual_success_after_retry(self, store_with_mock_embeddings):
        """Test that Qdrant sync succeeds after transient failure."""
        call_count = 0

        async def flaky_upsert(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Transient error")
            # Succeed on second attempt
            return MagicMock()

        original_upsert = store_with_mock_embeddings.qdrant_client.upsert
        store_with_mock_embeddings.qdrant_client.upsert = flaky_upsert

        # Should succeed after retry
        await store_with_mock_embeddings.aput(
            ("test",),
            "item",
            {"content": "test data"},
            wait_for_vector_sync=True
        )

        # Verify retry happened
        assert call_count == 2

        # Restore
        store_with_mock_embeddings.qdrant_client.upsert = original_upsert


@pytest.mark.integration
class TestCircuitBreaker:
    """Test circuit breaker behavior."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_failures(self):
        """Test circuit breaker opens after threshold failures."""
        from vertector_scylladbstore import CircuitBreaker, StoreConnectionError

        # Create circuit breaker with low threshold for testing
        cb = CircuitBreaker(
            failure_threshold=2,
            success_threshold=2,
            timeout_seconds=5.0
        )

        # Define a failing function
        async def failing_func():
            raise Exception("Simulated error")

        # Initial state should be CLOSED
        state = cb.get_state()
        assert state["state"] == "CLOSED"

        # Trigger failures to open circuit
        for i in range(3):
            try:
                await cb.call(failing_func)
            except Exception:
                pass

        # Circuit should be OPEN after threshold exceeded
        state = cb.get_state()
        assert state["state"] == "OPEN"
        assert state["failure_count"] >= 2

        # Verify circuit is blocking requests
        with pytest.raises(StoreConnectionError):
            await cb.call(failing_func)

    @pytest.mark.asyncio
    async def test_circuit_breaker_half_open_state(self):
        """Test circuit breaker transitions to half-open state."""
        from vertector_scylladbstore import CircuitBreaker
        import time

        cb = CircuitBreaker(
            failure_threshold=1,
            success_threshold=1,
            timeout_seconds=1.0  # Short timeout for testing
        )

        # Force circuit to open
        async def failing_func():
            raise Exception("Simulated error")

        # Trigger failure to open circuit
        try:
            await cb.call(failing_func)
        except:
            pass

        state = cb.get_state()
        assert state["state"] == "OPEN"

        # Wait for timeout
        await asyncio.sleep(1.5)

        # Create a successful function
        async def success_func():
            return "success"

        # Try an operation - this should trigger transition to half-open then potentially close
        try:
            result = await cb.call(success_func)
        except:
            pass  # May fail if still testing

        # Check state - should be HALF_OPEN or CLOSED (if success threshold met)
        state = cb.get_state()
        assert state["state"] in ["HALF_OPEN", "CLOSED"]


@pytest.mark.integration
class TestRetryLogic:
    """Test retry logic for transient failures."""

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, store_with_mock_embeddings):
        """Test that operations retry on transient errors."""
        retry_count = 0

        async def flaky_operation(*args, **kwargs):
            nonlocal retry_count
            retry_count += 1
            if retry_count < 2:
                raise Exception("Transient error")
            return None  # Success

        # Test with Qdrant sync (has retry logic)
        original_upsert = store_with_mock_embeddings.qdrant_client.upsert
        store_with_mock_embeddings.qdrant_client.upsert = flaky_operation

        # Should succeed after retry
        await store_with_mock_embeddings.aput(
            ("test",),
            "item",
            {"content": "test"},
            wait_for_vector_sync=True
        )

        assert retry_count == 2  # Failed once, succeeded on retry

        # Restore
        store_with_mock_embeddings.qdrant_client.upsert = original_upsert

    @pytest.mark.asyncio
    async def test_retry_exhaustion_raises_error(self, store_with_mock_embeddings):
        """Test that error is raised after all retries exhausted."""
        from vertector_scylladbstore import StoreQueryError

        async def always_failing(*args, **kwargs):
            raise Exception("Permanent error")

        original_upsert = store_with_mock_embeddings.qdrant_client.upsert
        store_with_mock_embeddings.qdrant_client.upsert = always_failing

        # Should raise after exhausting retries
        with pytest.raises(StoreQueryError):
            await store_with_mock_embeddings.aput(
                ("test",),
                "item",
                {"content": "test"},
                wait_for_vector_sync=True
            )

        # Restore
        store_with_mock_embeddings.qdrant_client.upsert = original_upsert


@pytest.mark.unit
class TestConfigValidation:
    """Test configuration validation (if using config module)."""

    def test_auth_config_validation(self):
        """Test authentication config validation."""
        from vertector_scylladbstore.config import AuthConfig
        from pydantic import ValidationError

        # Valid config
        config = AuthConfig(
            enabled=True,
            username="test_user",
            password="secret"
        )
        assert config.enabled
        assert config.username == "test_user"

        # Invalid: enabled but no username
        with pytest.raises(ValidationError):
            AuthConfig(
                enabled=True,
                password="secret"
                # Missing username
            )

        # Invalid: enabled but no password or secret
        with pytest.raises(ValidationError):
            AuthConfig(
                enabled=True,
                username="test_user"
                # Missing password and password_secret_name
            )

    def test_tls_config_validation(self):
        """Test TLS config validation."""
        from vertector_scylladbstore.config import TLSConfig
        from pydantic import ValidationError

        # Valid disabled config
        config = TLSConfig(enabled=False)
        assert not config.enabled

        # Invalid: CERT_REQUIRED without ca_cert_file
        with pytest.raises(ValidationError):
            TLSConfig(
                enabled=True,
                verify_mode="CERT_REQUIRED"
                # Missing ca_cert_file
            )

    def test_retry_config_validation(self):
        """Test retry config validation."""
        from vertector_scylladbstore.config import RetryConfig
        from pydantic import ValidationError

        # Valid config
        config = RetryConfig(
            max_retries=5,
            initial_delay=1.0,
            backoff_factor=2.0
        )
        assert config.max_retries == 5

        # Invalid: max_retries out of range
        with pytest.raises(ValidationError):
            RetryConfig(max_retries=100)  # Exceeds max of 10

        # Invalid: negative initial_delay
        with pytest.raises(ValidationError):
            RetryConfig(initial_delay=-1.0)

    def test_pool_config_validation(self):
        """Test pool config validation."""
        from vertector_scylladbstore.config import PoolConfig
        from pydantic import ValidationError

        # Valid config
        config = PoolConfig(
            core_connections=2,
            max_connections=10
        )
        assert config.core_connections == 2

        # Invalid: core > max
        with pytest.raises(ValidationError):
            PoolConfig(
                core_connections=20,
                max_connections=10
            )
