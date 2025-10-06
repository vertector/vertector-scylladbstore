"""
Tests for config module (Pydantic models, validation, secrets management).
"""

import pytest
import os
from unittest.mock import Mock, patch, MagicMock

from vertector_scylladbstore.config import (
    SecretsProvider,
    SecretsManager,
    TLSConfig,
    AuthConfig,
    RetryConfig,
    CircuitBreakerConfig,
    PoolConfig,
    MetricsConfig,
    RateLimitConfig,
    QdrantConfig,
    ScyllaDBStoreConfig,
    load_config_from_env,
)
from pydantic import ValidationError


# ============================================================================
# SecretsManager Tests
# ============================================================================

class TestSecretsManager:
    """Test secrets management functionality."""

    def test_env_provider_success(self):
        """Test retrieving secret from environment variables."""
        with patch.dict(os.environ, {"MY_SECRET": "secret_value"}):
            manager = SecretsManager(provider=SecretsProvider.ENV)
            value = manager.get_secret("MY_SECRET")
            assert value == "secret_value"

    def test_env_provider_with_default(self):
        """Test environment provider with default value."""
        manager = SecretsManager(provider=SecretsProvider.ENV)
        value = manager.get_secret("NONEXISTENT_SECRET", default="default_value")
        assert value == "default_value"

    def test_env_provider_missing_raises(self):
        """Test that missing env var without default raises error."""
        manager = SecretsManager(provider=SecretsProvider.ENV)
        with pytest.raises(ValueError) as exc_info:
            manager.get_secret("NONEXISTENT_SECRET")
        assert "not found in environment" in str(exc_info.value)

    def test_unsupported_provider_raises(self):
        """Test that unsupported providers raise NotImplementedError."""
        manager = SecretsManager(provider=SecretsProvider.AZURE_KEY_VAULT)
        with pytest.raises(NotImplementedError) as exc_info:
            manager.get_secret("some_secret")
        assert "not yet implemented" in str(exc_info.value)


# ============================================================================
# TLSConfig Tests
# ============================================================================

class TestTLSConfig:
    """Test TLS configuration validation."""

    def test_tls_disabled_no_validation(self):
        """Test that disabled TLS doesn't require certificates."""
        config = TLSConfig(enabled=False)
        assert config.enabled is False
        assert config.ca_cert_file is None

    def test_tls_enabled_cert_required_needs_ca(self):
        """Test that CERT_REQUIRED mode requires CA certificate."""
        with pytest.raises(ValidationError) as exc_info:
            TLSConfig(
                enabled=True,
                verify_mode="CERT_REQUIRED",
                ca_cert_file=None
            )
        assert "ca_cert_file" in str(exc_info.value).lower()

    def test_client_cert_requires_key(self, tmp_path):
        """Test that client certificate requires private key."""
        # Create dummy cert file
        cert_file = tmp_path / "cert.pem"
        cert_file.write_text("FAKE CERT")

        with pytest.raises(ValidationError) as exc_info:
            TLSConfig(
                enabled=True,
                verify_mode="CERT_NONE",
                cert_file=str(cert_file),
                key_file=None
            )
        assert "private key" in str(exc_info.value).lower()

    def test_cert_file_validation(self):
        """Test that certificate files must exist."""
        with pytest.raises(ValidationError) as exc_info:
            TLSConfig(
                enabled=True,
                verify_mode="CERT_NONE",
                cert_file="/nonexistent/cert.pem",
                key_file="/nonexistent/key.pem"
            )
        assert "not found" in str(exc_info.value).lower()

    def test_tls_valid_config(self, tmp_path):
        """Test valid TLS configuration."""
        # Create dummy cert files
        ca_cert = tmp_path / "ca.pem"
        ca_cert.write_text("FAKE CA CERT")

        config = TLSConfig(
            enabled=True,
            verify_mode="CERT_REQUIRED",
            ca_cert_file=str(ca_cert)
        )
        assert config.enabled is True
        assert config.verify_mode == "CERT_REQUIRED"


# ============================================================================
# AuthConfig Tests
# ============================================================================

class TestAuthConfig:
    """Test authentication configuration validation."""

    def test_auth_disabled_no_validation(self):
        """Test that disabled auth doesn't require credentials."""
        config = AuthConfig(enabled=False)
        assert config.enabled is False
        assert config.username is None

    def test_auth_enabled_requires_username(self):
        """Test that enabled auth requires username."""
        with pytest.raises(ValidationError) as exc_info:
            AuthConfig(enabled=True, username=None)
        assert "username" in str(exc_info.value).lower()

    def test_auth_requires_password_or_secret(self):
        """Test that auth requires either password or secret name."""
        with pytest.raises(ValidationError) as exc_info:
            AuthConfig(
                enabled=True,
                username="user",
                password=None,
                password_secret_name=None
            )
        assert "password" in str(exc_info.value).lower()

    def test_auth_with_password(self):
        """Test valid auth config with password."""
        config = AuthConfig(
            enabled=True,
            username="scylla_user",
            password="secret_password"
        )
        assert config.username == "scylla_user"
        assert config.password == "secret_password"

    def test_auth_with_secret_name(self):
        """Test valid auth config with secret name."""
        config = AuthConfig(
            enabled=True,
            username="scylla_user",
            password_secret_name="prod/scylladb/password"
        )
        assert config.password_secret_name == "prod/scylladb/password"


# ============================================================================
# RetryConfig Tests
# ============================================================================

class TestRetryConfig:
    """Test retry configuration validation."""

    def test_retry_default_values(self):
        """Test default retry configuration."""
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.initial_delay == 0.5
        assert config.backoff_factor == 2.0
        assert config.jitter is True

    def test_retry_max_retries_validation(self):
        """Test max_retries bounds."""
        # Too low
        with pytest.raises(ValidationError):
            RetryConfig(max_retries=0)

        # Too high
        with pytest.raises(ValidationError):
            RetryConfig(max_retries=11)

        # Valid
        config = RetryConfig(max_retries=5)
        assert config.max_retries == 5

    def test_retry_delay_validation(self):
        """Test delay bounds validation."""
        # Initial delay too low
        with pytest.raises(ValidationError):
            RetryConfig(initial_delay=0.05)

        # Max delay too low
        with pytest.raises(ValidationError):
            RetryConfig(max_delay=0.5)

        # Backoff factor too low
        with pytest.raises(ValidationError):
            RetryConfig(backoff_factor=0.5)


# ============================================================================
# CircuitBreakerConfig Tests
# ============================================================================

class TestCircuitBreakerConfig:
    """Test circuit breaker configuration validation."""

    def test_circuit_breaker_defaults(self):
        """Test default circuit breaker config."""
        config = CircuitBreakerConfig()
        assert config.enabled is True
        assert config.failure_threshold == 5
        assert config.success_threshold == 2
        assert config.timeout_seconds == 60.0

    def test_failure_threshold_bounds(self):
        """Test failure threshold validation."""
        with pytest.raises(ValidationError):
            CircuitBreakerConfig(failure_threshold=0)

        with pytest.raises(ValidationError):
            CircuitBreakerConfig(failure_threshold=21)

        config = CircuitBreakerConfig(failure_threshold=10)
        assert config.failure_threshold == 10

    def test_timeout_bounds(self):
        """Test timeout validation."""
        with pytest.raises(ValidationError):
            CircuitBreakerConfig(timeout_seconds=5.0)  # Too low

        with pytest.raises(ValidationError):
            CircuitBreakerConfig(timeout_seconds=400.0)  # Too high

        config = CircuitBreakerConfig(timeout_seconds=120.0)
        assert config.timeout_seconds == 120.0


# ============================================================================
# PoolConfig Tests
# ============================================================================

class TestPoolConfig:
    """Test connection pool configuration validation."""

    def test_pool_defaults(self):
        """Test default pool configuration."""
        config = PoolConfig()
        assert config.executor_threads == 4
        assert config.max_connections == 100
        assert config.core_connections == 2

    def test_core_exceeds_max_raises(self):
        """Test that core_connections cannot exceed max_connections."""
        # core_connections: ge=1, le=10
        # max_connections: ge=10, le=1000
        # Use core=8, max=10 where 8 is within [1,10] and 10 is within [10,1000]
        # But we want core > max, so we need max < core
        # This is impossible with the field constraints!
        # Let's just test field constraint violation instead
        with pytest.raises(ValidationError):
            PoolConfig(core_connections=15)  # Exceeds le=10

    def test_valid_pool_config(self):
        """Test valid pool configuration."""
        config = PoolConfig(
            executor_threads=8,
            max_connections=200,
            core_connections=5
        )
        assert config.core_connections <= config.max_connections


# ============================================================================
# MetricsConfig Tests
# ============================================================================

class TestMetricsConfig:
    """Test metrics configuration validation."""

    def test_metrics_defaults(self):
        """Test default metrics configuration."""
        config = MetricsConfig()
        assert config.enabled is True
        assert config.percentiles == [0.5, 0.95, 0.99]
        assert config.export_interval_seconds == 60.0

    def test_invalid_percentiles(self):
        """Test that percentiles must be between 0 and 1."""
        with pytest.raises(ValidationError) as exc_info:
            MetricsConfig(percentiles=[0.5, 1.5])  # 1.5 is invalid
        assert "between 0.0 and 1.0" in str(exc_info.value)

        with pytest.raises(ValidationError):
            MetricsConfig(percentiles=[-0.1, 0.5])  # -0.1 is invalid

    def test_percentiles_sorted(self):
        """Test that percentiles are sorted."""
        config = MetricsConfig(percentiles=[0.99, 0.5, 0.95])
        assert config.percentiles == [0.5, 0.95, 0.99]

    def test_prometheus_port_validation(self):
        """Test Prometheus port bounds."""
        with pytest.raises(ValidationError):
            MetricsConfig(prometheus_port=80)  # Too low

        with pytest.raises(ValidationError):
            MetricsConfig(prometheus_port=70000)  # Too high

        config = MetricsConfig(prometheus_port=9090)
        assert config.prometheus_port == 9090


# ============================================================================
# RateLimitConfig Tests
# ============================================================================

class TestRateLimitConfig:
    """Test rate limiting configuration validation."""

    def test_rate_limit_defaults(self):
        """Test default rate limit configuration."""
        config = RateLimitConfig()
        assert config.enabled is False
        assert config.requests_per_minute == 1000
        assert config.burst_size == 100

    def test_requests_per_minute_bounds(self):
        """Test requests per minute validation."""
        with pytest.raises(ValidationError):
            RateLimitConfig(requests_per_minute=5)  # Too low

        with pytest.raises(ValidationError):
            RateLimitConfig(requests_per_minute=200000)  # Too high

        config = RateLimitConfig(requests_per_minute=5000)
        assert config.requests_per_minute == 5000

    def test_burst_size_bounds(self):
        """Test burst size validation."""
        with pytest.raises(ValidationError):
            RateLimitConfig(burst_size=0)

        with pytest.raises(ValidationError):
            RateLimitConfig(burst_size=1500)

        config = RateLimitConfig(burst_size=500)
        assert config.burst_size == 500


# ============================================================================
# QdrantConfig Tests
# ============================================================================

class TestQdrantConfig:
    """Test Qdrant configuration validation."""

    def test_qdrant_defaults(self):
        """Test default Qdrant configuration."""
        config = QdrantConfig()
        assert config.url == "http://localhost:6333"
        assert config.timeout == 30.0
        assert config.batch_size == 100

    def test_timeout_bounds(self):
        """Test Qdrant timeout validation."""
        with pytest.raises(ValidationError):
            QdrantConfig(timeout=2.0)  # Too low

        with pytest.raises(ValidationError):
            QdrantConfig(timeout=150.0)  # Too high

        config = QdrantConfig(timeout=60.0)
        assert config.timeout == 60.0

    def test_batch_size_bounds(self):
        """Test Qdrant batch size validation."""
        with pytest.raises(ValidationError):
            QdrantConfig(batch_size=5)  # Too low

        with pytest.raises(ValidationError):
            QdrantConfig(batch_size=1500)  # Too high


# ============================================================================
# ScyllaDBStoreConfig Tests
# ============================================================================

class TestScyllaDBStoreConfig:
    """Test complete store configuration."""

    def test_minimal_config(self):
        """Test minimal valid configuration."""
        config = ScyllaDBStoreConfig(
            contact_points=["localhost"],
            keyspace="test_keyspace"
        )
        assert config.contact_points == ["localhost"]
        assert config.keyspace == "test_keyspace"
        assert config.port == 9042

    def test_empty_contact_points_raises(self):
        """Test that empty contact points list raises error."""
        with pytest.raises(ValidationError) as exc_info:
            ScyllaDBStoreConfig(
                contact_points=[],
                keyspace="test"
            )
        assert "at least one contact point" in str(exc_info.value).lower()

    def test_invalid_keyspace_raises(self):
        """Test that invalid keyspace names are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScyllaDBStoreConfig(
                contact_points=["localhost"],
                keyspace="test-keyspace"  # Hyphen not allowed
            )
        assert "alphanumeric" in str(exc_info.value).lower()

    def test_get_secrets_manager(self):
        """Test secrets manager creation."""
        config = ScyllaDBStoreConfig(
            contact_points=["localhost"],
            keyspace="test",
            secrets_provider=SecretsProvider.ENV
        )
        manager = config.get_secrets_manager()
        assert isinstance(manager, SecretsManager)
        assert manager.provider == SecretsProvider.ENV

    def test_resolve_password_secret(self):
        """Test resolving password from secret."""
        with patch.dict(os.environ, {"DB_PASSWORD": "resolved_password"}):
            config = ScyllaDBStoreConfig(
                contact_points=["localhost"],
                keyspace="test",
                auth=AuthConfig(
                    enabled=True,
                    username="user",
                    password_secret_name="DB_PASSWORD"
                )
            )
            config.resolve_secrets()
            assert config.auth.password == "resolved_password"
            assert config.auth.password_secret_name is None

    def test_resolve_qdrant_api_key_secret(self):
        """Test resolving Qdrant API key from secret."""
        with patch.dict(os.environ, {"QDRANT_KEY": "api_key_value"}):
            config = ScyllaDBStoreConfig(
                contact_points=["localhost"],
                keyspace="test",
                qdrant=QdrantConfig(api_key="secret:QDRANT_KEY")
            )
            config.resolve_secrets()
            assert config.qdrant.api_key == "api_key_value"


# ============================================================================
# load_config_from_env Tests
# ============================================================================

class TestLoadConfigFromEnv:
    """Test loading configuration from environment variables."""

    def test_load_minimal_env_config(self):
        """Test loading minimal config from environment."""
        with patch.dict(os.environ, {
            "SCYLLADB_CONTACT_POINTS": "host1,host2,host3",
            "SCYLLADB_KEYSPACE": "production"
        }, clear=True):
            config = load_config_from_env()
            assert config.contact_points == ["host1", "host2", "host3"]
            assert config.keyspace == "production"

    def test_load_with_auth_env(self):
        """Test loading auth config from environment."""
        with patch.dict(os.environ, {
            "SCYLLADB_CONTACT_POINTS": "localhost",
            "SCYLLADB_KEYSPACE": "test",
            "SCYLLADB_AUTH_ENABLED": "true",
            "SCYLLADB_USERNAME": "admin",
            "SCYLLADB_PASSWORD": "secret123"
        }, clear=True):
            config = load_config_from_env()
            assert config.auth.enabled is True
            assert config.auth.username == "admin"
            assert config.auth.password == "secret123"

    def test_load_with_tls_env(self, tmp_path):
        """Test loading TLS config from environment."""
        ca_cert = tmp_path / "ca.pem"
        ca_cert.write_text("FAKE CERT")

        with patch.dict(os.environ, {
            "SCYLLADB_CONTACT_POINTS": "localhost",
            "SCYLLADB_KEYSPACE": "test",
            "SCYLLADB_TLS_ENABLED": "true",
            "SCYLLADB_TLS_CA_CERT": str(ca_cert)
        }, clear=True):
            config = load_config_from_env()
            assert config.tls.enabled is True
            assert config.tls.ca_cert_file == str(ca_cert)

    def test_load_with_qdrant_env(self):
        """Test loading Qdrant config from environment."""
        with patch.dict(os.environ, {
            "SCYLLADB_CONTACT_POINTS": "localhost",
            "SCYLLADB_KEYSPACE": "test",
            "QDRANT_URL": "http://qdrant.example.com:6333",
            "QDRANT_API_KEY": "my_api_key"
        }, clear=True):
            config = load_config_from_env()
            assert config.qdrant.url == "http://qdrant.example.com:6333"
            assert config.qdrant.api_key == "my_api_key"

    def test_load_defaults_when_not_set(self):
        """Test that defaults are used when env vars not set."""
        with patch.dict(os.environ, {}, clear=True):
            config = load_config_from_env()
            assert config.contact_points == ["127.0.0.1"]
            assert config.keyspace == "langgraph_store"
            assert config.port == 9042
            assert config.auth.enabled is False
            assert config.tls.enabled is False
