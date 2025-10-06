"""
Production-ready configuration management for AsyncScyllaDBStore.

This module provides:
- Pydantic-based configuration validation
- Secrets management integration (AWS Secrets Manager, environment variables)
- TLS/SSL configuration
- Authentication configuration
- Retry and circuit breaker configuration
"""

import os
import json
import logging
from typing import Any, Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
from enum import Enum

logger = logging.getLogger(__name__)


# ============================================================================
# Secrets Management
# ============================================================================

class SecretsProvider(str, Enum):
    """Supported secrets management providers."""
    ENV = "env"
    AWS_SECRETS_MANAGER = "aws_secrets_manager"
    AZURE_KEY_VAULT = "azure_key_vault"
    HASHICORP_VAULT = "hashicorp_vault"


class SecretsManager:
    """
    Unified interface for retrieving secrets from various providers.

    Supports:
    - Environment variables (default)
    - AWS Secrets Manager
    - Azure Key Vault (future)
    - HashiCorp Vault (future)
    """

    def __init__(self, provider: SecretsProvider = SecretsProvider.ENV):
        self.provider = provider
        self._aws_client = None

    def get_secret(self, secret_name: str, default: Optional[str] = None) -> Optional[str]:
        """
        Retrieve a secret from the configured provider.

        Args:
            secret_name: Name/key of the secret
            default: Default value if secret not found

        Returns:
            Secret value or default

        Raises:
            ValueError: If secret not found and no default provided
        """
        if self.provider == SecretsProvider.ENV:
            return self._get_from_env(secret_name, default)
        elif self.provider == SecretsProvider.AWS_SECRETS_MANAGER:
            return self._get_from_aws(secret_name, default)
        else:
            raise NotImplementedError(f"Provider {self.provider} not yet implemented")

    def _get_from_env(self, secret_name: str, default: Optional[str] = None) -> Optional[str]:
        """Get secret from environment variable."""
        value = os.getenv(secret_name, default)
        if value is None:
            raise ValueError(f"Secret '{secret_name}' not found in environment variables")
        return value

    def _get_from_aws(self, secret_name: str, default: Optional[str] = None) -> Optional[str]:
        """Get secret from AWS Secrets Manager."""
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError:
            raise ImportError(
                "AWS Secrets Manager requires boto3. Install with: pip install boto3"
            )

        if self._aws_client is None:
            self._aws_client = boto3.client('secretsmanager')

        try:
            response = self._aws_client.get_secret_value(SecretId=secret_name)

            # Parse secret value (could be string or JSON)
            secret_value = response.get('SecretString')
            if secret_value:
                try:
                    # Try parsing as JSON
                    secret_dict = json.loads(secret_value)
                    # If it's a dict, return the whole thing as JSON string
                    return json.dumps(secret_dict)
                except json.JSONDecodeError:
                    # Plain string secret
                    return secret_value

            # Binary secret
            return response.get('SecretBinary')

        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'ResourceNotFoundException':
                if default is not None:
                    logger.warning(f"Secret '{secret_name}' not found in AWS, using default")
                    return default
                raise ValueError(f"Secret '{secret_name}' not found in AWS Secrets Manager")
            else:
                raise ValueError(f"Failed to retrieve secret '{secret_name}': {e}")


# ============================================================================
# Configuration Models
# ============================================================================

class TLSConfig(BaseModel):
    """TLS/SSL configuration for secure connections."""

    enabled: bool = Field(
        default=False,
        description="Enable TLS/SSL encryption"
    )

    cert_file: Optional[str] = Field(
        default=None,
        description="Path to client certificate file"
    )

    key_file: Optional[str] = Field(
        default=None,
        description="Path to client private key file"
    )

    ca_cert_file: Optional[str] = Field(
        default=None,
        description="Path to CA certificate file for server verification"
    )

    verify_mode: Literal["CERT_NONE", "CERT_OPTIONAL", "CERT_REQUIRED"] = Field(
        default="CERT_REQUIRED",
        description="Certificate verification mode"
    )

    protocol_version: Literal["TLSv1_2", "TLSv1_3"] = Field(
        default="TLSv1_2",
        description="TLS protocol version"
    )

    @field_validator('cert_file', 'key_file', 'ca_cert_file')
    @classmethod
    def validate_file_exists(cls, v):
        """Validate that certificate files exist."""
        if v is not None and not os.path.exists(v):
            raise ValueError(f"Certificate file not found: {v}")
        return v

    @model_validator(mode='after')
    def validate_tls_config(self):
        """Validate TLS configuration consistency."""
        if self.enabled:
            # If TLS is enabled, require CA cert for verification
            if self.verify_mode == "CERT_REQUIRED" and not self.ca_cert_file:
                raise ValueError(
                    "TLS verification requires 'ca_cert_file' when verify_mode is CERT_REQUIRED"
                )

            # If client cert is provided, key must also be provided
            if self.cert_file and not self.key_file:
                raise ValueError("Client certificate requires private key file")

        return self


class AuthConfig(BaseModel):
    """Authentication configuration for ScyllaDB."""

    enabled: bool = Field(
        default=False,
        description="Enable authentication"
    )

    username: Optional[str] = Field(
        default=None,
        description="Database username (can use secrets manager)"
    )

    password: Optional[str] = Field(
        default=None,
        description="Database password (can use secrets manager)"
    )

    password_secret_name: Optional[str] = Field(
        default=None,
        description="Secret name for password (if using secrets manager)"
    )

    @model_validator(mode='after')
    def validate_auth_config(self):
        """Validate authentication configuration."""
        if self.enabled:
            if not self.username:
                raise ValueError("Authentication requires username")

            # Must have either password or password_secret_name
            if not self.password and not self.password_secret_name:
                raise ValueError(
                    "Authentication requires either 'password' or 'password_secret_name'"
                )

        return self


class RetryConfig(BaseModel):
    """Retry configuration for resilient operations."""

    max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of retry attempts"
    )

    initial_delay: float = Field(
        default=0.5,
        ge=0.1,
        le=5.0,
        description="Initial retry delay in seconds"
    )

    backoff_factor: float = Field(
        default=2.0,
        ge=1.0,
        le=10.0,
        description="Exponential backoff multiplier"
    )

    max_delay: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="Maximum retry delay in seconds"
    )

    jitter: bool = Field(
        default=True,
        description="Add random jitter to retry delays"
    )


class CircuitBreakerConfig(BaseModel):
    """Circuit breaker configuration for fault tolerance."""

    enabled: bool = Field(
        default=True,
        description="Enable circuit breaker"
    )

    failure_threshold: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of failures before opening circuit"
    )

    success_threshold: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Number of successes before closing circuit"
    )

    timeout_seconds: float = Field(
        default=60.0,
        ge=10.0,
        le=300.0,
        description="Seconds to wait before attempting recovery"
    )


class PoolConfig(BaseModel):
    """Connection pool configuration."""

    executor_threads: int = Field(
        default=4,
        ge=2,
        le=32,
        description="Number of executor threads for blocking operations"
    )

    max_connections: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Maximum number of connections per host"
    )

    core_connections: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Core connections per host"
    )

    @model_validator(mode='after')
    def validate_pool_config(self):
        """Validate pool configuration consistency."""
        if self.core_connections > self.max_connections:
            raise ValueError("core_connections cannot exceed max_connections")
        return self


class MetricsConfig(BaseModel):
    """Metrics and monitoring configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable metrics collection"
    )

    percentiles: list[float] = Field(
        default=[0.5, 0.95, 0.99],
        description="Latency percentiles to track (p50, p95, p99)"
    )

    prometheus_port: Optional[int] = Field(
        default=None,
        ge=1024,
        le=65535,
        description="Port for Prometheus metrics endpoint"
    )

    export_interval_seconds: float = Field(
        default=60.0,
        ge=10.0,
        le=300.0,
        description="Metrics export interval"
    )

    @field_validator('percentiles')
    @classmethod
    def validate_percentiles(cls, v):
        """Validate percentile values."""
        for p in v:
            if not 0.0 <= p <= 1.0:
                raise ValueError(f"Percentile must be between 0.0 and 1.0, got {p}")
        return sorted(v)


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""

    enabled: bool = Field(
        default=False,
        description="Enable rate limiting"
    )

    requests_per_minute: int = Field(
        default=1000,
        ge=10,
        le=100000,
        description="Maximum requests per minute"
    )

    burst_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum burst size"
    )


class QdrantConfig(BaseModel):
    """Qdrant vector database configuration."""

    url: str = Field(
        default="http://localhost:6333",
        description="Qdrant server URL"
    )

    collection: Optional[str] = Field(
        default=None,
        description="Qdrant collection name (defaults to keyspace)"
    )

    api_key: Optional[str] = Field(
        default=None,
        description="Qdrant API key for authentication"
    )

    timeout: float = Field(
        default=30.0,
        ge=5.0,
        le=120.0,
        description="Request timeout in seconds"
    )

    batch_size: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Batch size for bulk operations"
    )


class ScyllaDBStoreConfig(BaseModel):
    """
    Complete configuration for production-ready AsyncScyllaDBStore.

    Example usage:
        # From environment variables
        config = ScyllaDBStoreConfig(
            contact_points=["scylla1.example.com", "scylla2.example.com"],
            keyspace="production",
            auth=AuthConfig(
                enabled=True,
                username="scylla_user",
                password_secret_name="prod/scylladb/password"
            ),
            tls=TLSConfig(
                enabled=True,
                ca_cert_file="/etc/ssl/certs/ca.pem"
            )
        )

        # Create store with config
        store = await AsyncScyllaDBStore.from_config(config)
    """

    # Connection settings
    contact_points: list[str] = Field(
        description="ScyllaDB contact points (hostnames or IPs)"
    )

    keyspace: str = Field(
        description="ScyllaDB keyspace name"
    )

    port: int = Field(
        default=9042,
        ge=1,
        le=65535,
        description="ScyllaDB port"
    )

    # Security
    auth: AuthConfig = Field(
        default_factory=AuthConfig,
        description="Authentication configuration"
    )

    tls: TLSConfig = Field(
        default_factory=TLSConfig,
        description="TLS/SSL configuration"
    )

    # Resilience
    retry: RetryConfig = Field(
        default_factory=RetryConfig,
        description="Retry configuration"
    )

    circuit_breaker: CircuitBreakerConfig = Field(
        default_factory=CircuitBreakerConfig,
        description="Circuit breaker configuration"
    )

    # Performance
    pool: PoolConfig = Field(
        default_factory=PoolConfig,
        description="Connection pool configuration"
    )

    # Monitoring
    metrics: MetricsConfig = Field(
        default_factory=MetricsConfig,
        description="Metrics configuration"
    )

    # Rate limiting
    rate_limit: RateLimitConfig = Field(
        default_factory=RateLimitConfig,
        description="Rate limiting configuration"
    )

    # Qdrant integration
    qdrant: QdrantConfig = Field(
        default_factory=QdrantConfig,
        description="Qdrant configuration"
    )

    # Secrets management
    secrets_provider: SecretsProvider = Field(
        default=SecretsProvider.ENV,
        description="Secrets management provider"
    )

    model_config = ConfigDict(
        use_enum_values=True,
        validate_assignment=True
    )

    @field_validator('contact_points')
    @classmethod
    def validate_contact_points(cls, v):
        """Validate contact points."""
        if not v:
            raise ValueError("At least one contact point required")
        return v

    @field_validator('keyspace')
    @classmethod
    def validate_keyspace(cls, v):
        """Validate keyspace name."""
        if not v or not v.replace('_', '').isalnum():
            raise ValueError(
                "Keyspace must be alphanumeric with optional underscores"
            )
        return v

    def get_secrets_manager(self) -> SecretsManager:
        """Get configured secrets manager instance."""
        return SecretsManager(provider=self.secrets_provider)

    def resolve_secrets(self) -> None:
        """
        Resolve all secret references using the configured secrets manager.

        This should be called after loading config to replace secret references
        with actual values from the secrets provider.
        """
        secrets_manager = self.get_secrets_manager()

        # Resolve password from secret if needed
        if self.auth.enabled and self.auth.password_secret_name:
            self.auth.password = secrets_manager.get_secret(
                self.auth.password_secret_name
            )
            # Clear secret name to avoid confusion
            self.auth.password_secret_name = None

        # Resolve Qdrant API key if provided as secret
        if self.qdrant.api_key and self.qdrant.api_key.startswith("secret:"):
            secret_name = self.qdrant.api_key[7:]  # Remove "secret:" prefix
            self.qdrant.api_key = secrets_manager.get_secret(secret_name)


def load_config_from_env() -> ScyllaDBStoreConfig:
    """
    Load configuration from environment variables.

    Environment variables:
        SCYLLADB_CONTACT_POINTS: Comma-separated list of contact points
        SCYLLADB_KEYSPACE: Keyspace name
        SCYLLADB_PORT: Port (default: 9042)
        SCYLLADB_AUTH_ENABLED: Enable authentication (true/false)
        SCYLLADB_USERNAME: Database username
        SCYLLADB_PASSWORD: Database password (not recommended - use secret)
        SCYLLADB_PASSWORD_SECRET: Secret name for password
        SCYLLADB_TLS_ENABLED: Enable TLS (true/false)
        SCYLLADB_TLS_CA_CERT: Path to CA certificate
        SCYLLADB_TLS_CERT: Path to client certificate
        SCYLLADB_TLS_KEY: Path to client key
        QDRANT_URL: Qdrant server URL
        QDRANT_API_KEY: Qdrant API key
        SECRETS_PROVIDER: Secrets provider (env, aws_secrets_manager)

    Returns:
        Validated configuration
    """
    contact_points_str = os.getenv("SCYLLADB_CONTACT_POINTS", "127.0.0.1")
    contact_points = [cp.strip() for cp in contact_points_str.split(",")]

    config = ScyllaDBStoreConfig(
        contact_points=contact_points,
        keyspace=os.getenv("SCYLLADB_KEYSPACE", "langgraph_store"),
        port=int(os.getenv("SCYLLADB_PORT", "9042")),
        auth=AuthConfig(
            enabled=os.getenv("SCYLLADB_AUTH_ENABLED", "false").lower() == "true",
            username=os.getenv("SCYLLADB_USERNAME"),
            password=os.getenv("SCYLLADB_PASSWORD"),
            password_secret_name=os.getenv("SCYLLADB_PASSWORD_SECRET"),
        ),
        tls=TLSConfig(
            enabled=os.getenv("SCYLLADB_TLS_ENABLED", "false").lower() == "true",
            ca_cert_file=os.getenv("SCYLLADB_TLS_CA_CERT"),
            cert_file=os.getenv("SCYLLADB_TLS_CERT"),
            key_file=os.getenv("SCYLLADB_TLS_KEY"),
        ),
        qdrant=QdrantConfig(
            url=os.getenv("QDRANT_URL", "http://localhost:6333"),
            api_key=os.getenv("QDRANT_API_KEY"),
        ),
        secrets_provider=SecretsProvider(
            os.getenv("SECRETS_PROVIDER", "env")
        ),
    )

    # Resolve secrets
    config.resolve_secrets()

    return config
