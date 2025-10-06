"""
Observability module for AsyncScyllaDBStore.

Provides:
- OpenTelemetry distributed tracing
- Enhanced metrics with percentile latencies
- Structured logging
- Alerting integration (PagerDuty, Slack)
"""

import time
import logging
import statistics
from typing import Any, Optional, Callable
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from functools import wraps
from enum import Enum

logger = logging.getLogger(__name__)


# ============================================================================
# OpenTelemetry Tracing
# ============================================================================

class TracingProvider(str, Enum):
    """Supported tracing providers."""
    NONE = "none"
    OPENTELEMETRY = "opentelemetry"


class Tracer:
    """
    Unified tracing interface with OpenTelemetry support.

    Supports:
    - OpenTelemetry instrumentation
    - Automatic span creation and context propagation
    - Graceful degradation when OpenTelemetry not available
    """

    def __init__(self, service_name: str = "scylladb-store", enabled: bool = True):
        self.service_name = service_name
        self.enabled = enabled
        self._tracer = None
        self._provider_type = TracingProvider.NONE

        if enabled:
            self._initialize_opentelemetry()

    def _initialize_opentelemetry(self):
        """Initialize OpenTelemetry tracing."""
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.resources import Resource, SERVICE_NAME

            # Check if already configured
            current_tracer = trace.get_tracer(__name__)
            if hasattr(current_tracer, '__class__') and 'ProxyTracer' not in str(current_tracer.__class__):
                # Already configured
                self._tracer = current_tracer
                self._provider_type = TracingProvider.OPENTELEMETRY
                logger.info("Using existing OpenTelemetry tracer")
                return

            # Configure new provider
            resource = Resource(attributes={SERVICE_NAME: self.service_name})
            provider = TracerProvider(resource=resource)

            # Try to add OTLP exporter if available
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                otlp_exporter = OTLPSpanExporter()
                provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
                logger.info("OpenTelemetry OTLP exporter configured")
            except ImportError:
                # OTLP not available, use console exporter for debugging
                try:
                    from opentelemetry.sdk.trace.export import ConsoleSpanExporter
                    console_exporter = ConsoleSpanExporter()
                    provider.add_span_processor(BatchSpanProcessor(console_exporter))
                    logger.info("OpenTelemetry console exporter configured (OTLP not available)")
                except ImportError:
                    logger.warning("No OpenTelemetry exporters available")

            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer(__name__)
            self._provider_type = TracingProvider.OPENTELEMETRY
            logger.info(f"OpenTelemetry tracing initialized for {self.service_name}")

        except ImportError:
            logger.warning(
                "OpenTelemetry not available. Install with: "
                "pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp"
            )
            self.enabled = False
            self._provider_type = TracingProvider.NONE

    @asynccontextmanager
    async def span(self, name: str, attributes: Optional[dict[str, Any]] = None):
        """
        Create a traced span for an operation.

        Args:
            name: Span name (e.g., "scylladb.put", "scylladb.search")
            attributes: Span attributes (metadata)

        Yields:
            Span context manager
        """
        if not self.enabled or self._tracer is None:
            # No-op span when tracing disabled
            yield None
            return

        from opentelemetry import trace

        with self._tracer.start_as_current_span(name) as span:
            if attributes:
                for key, value in attributes.items():
                    # Convert to string if needed (OpenTelemetry requirement)
                    span.set_attribute(key, str(value) if not isinstance(value, (str, int, float, bool)) else value)

            try:
                yield span
                # Set status to OK on successful completion
                span.set_status(trace.Status(trace.StatusCode.OK))
            except Exception as e:
                # Record exception in span and set status to ERROR
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

    def trace_async(self, span_name: str):
        """
        Decorator for tracing async functions.

        Usage:
            @tracer.trace_async("scylladb.put")
            async def aput(self, namespace, key, value):
                ...
        """
        def decorator(func: Callable):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                # Extract self for instance methods
                instance = args[0] if args and hasattr(args[0], '__class__') else None

                # Build attributes from function arguments
                attributes = {
                    "function": func.__name__,
                }

                # Add namespace and key if available (common pattern)
                if len(args) > 1:
                    if hasattr(args[1], '__iter__') and not isinstance(args[1], str):
                        attributes["namespace"] = str(args[1])
                    if len(args) > 2:
                        attributes["key"] = str(args[2])

                async with self.span(span_name, attributes):
                    return await func(*args, **kwargs)

            return wrapper
        return decorator


# ============================================================================
# Enhanced Metrics
# ============================================================================

class PercentileTracker:
    """
    Track percentile latencies (p50, p95, p99) efficiently.

    Uses a sliding window to avoid unbounded memory growth.
    """

    def __init__(self, window_size: int = 1000, percentiles: list[float] = None):
        """
        Initialize percentile tracker.

        Args:
            window_size: Number of recent samples to keep
            percentiles: Percentiles to track (e.g., [0.5, 0.95, 0.99])
        """
        self.window_size = window_size
        self.percentiles = percentiles or [0.5, 0.95, 0.99]
        self.samples = deque(maxlen=window_size)

    def record(self, value: float):
        """Record a sample."""
        self.samples.append(value)

    def get_percentiles(self) -> dict[str, float]:
        """
        Calculate percentile values.

        Returns:
            Dictionary with keys like "p50", "p95", "p99"
        """
        if not self.samples:
            return {f"p{int(p*100)}": 0.0 for p in self.percentiles}

        sorted_samples = sorted(self.samples)
        result = {}

        for p in self.percentiles:
            percentile_name = f"p{int(p*100)}"
            result[percentile_name] = statistics.quantiles(
                sorted_samples,
                n=100,
                method='inclusive'
            )[int(p * 100) - 1] if len(sorted_samples) > 1 else sorted_samples[0]

        return result

    def get_stats(self) -> dict[str, Any]:
        """
        Get comprehensive statistics.

        Returns:
            Dictionary with percentiles, avg, min, max, count
        """
        if not self.samples:
            return {
                "count": 0,
                "avg": 0.0,
                "min": 0.0,
                "max": 0.0,
                **{f"p{int(p*100)}": 0.0 for p in self.percentiles}
            }

        percentiles = self.get_percentiles()

        return {
            "count": len(self.samples),
            "avg": statistics.mean(self.samples),
            "min": min(self.samples),
            "max": max(self.samples),
            **percentiles
        }


class EnhancedMetrics:
    """
    Enhanced metrics with percentile latencies and operation tracking.

    Tracks:
    - Latency percentiles (p50, p95, p99)
    - Request rates
    - Error rates
    - Connection pool stats
    - Cache hit rates
    """

    def __init__(self, service_name: str = "scylladb_store", percentiles: list[float] = None):
        """
        Initialize metrics tracker.

        Args:
            service_name: Service name for identification
            percentiles: Percentiles to track (default: [0.5, 0.95, 0.99])
        """
        self.service_name = service_name
        self.percentiles = percentiles or [0.5, 0.95, 0.99]

        # Latency tracking by operation type
        self.latencies: dict[str, PercentileTracker] = defaultdict(
            lambda: PercentileTracker(percentiles=self.percentiles)
        )

        # Operation counters
        self.operation_counts: dict[str, int] = defaultdict(int)
        self.error_counts: dict[str, int] = defaultdict(int)
        self.error_types: dict[str, int] = defaultdict(int)

        # Cache metrics
        self.cache_hits = 0
        self.cache_misses = 0

        # Connection pool metrics
        self.active_connections = 0
        self.idle_connections = 0

        # Batch metrics
        self.batch_count = 0
        self.batch_operation_count = 0

        # Start time for rate calculations
        self.start_time = time.time()

    def record_latency(self, operation: str, latency_ms: float):
        """Record operation latency in milliseconds."""
        self.latencies[operation].record(latency_ms)
        self.operation_counts[operation] += 1

    def record_error(self, operation: str):
        """Record operation error."""
        self.error_counts[operation] += 1

    def record_cache_hit(self):
        """Record cache hit."""
        self.cache_hits += 1

    def record_cache_miss(self):
        """Record cache miss."""
        self.cache_misses += 1

    # Compatibility methods for existing QueryMetrics API
    def record_query(self, operation: str, latency_ms: float, success: bool = True, error_type: str | None = None):
        """
        Record a query execution (compatibility method for QueryMetrics API).

        Args:
            operation: Operation type (e.g., 'get', 'put', 'search')
            latency_ms: Query latency in milliseconds
            success: Whether the query succeeded
            error_type: Type of error if query failed
        """
        self.record_latency(operation, latency_ms)
        if not success:
            self.record_error(operation)
            if error_type:
                self.error_types[error_type] += 1

    def record_batch(self, batch_size: int, batch_type: str, latency_ms: float, success: bool = True):
        """
        Record a batch operation (compatibility method for QueryMetrics API).

        Args:
            batch_size: Number of operations in the batch
            batch_type: Type of batch execution
            latency_ms: Batch execution latency in milliseconds
            success: Whether the batch succeeded
        """
        self.batch_count += 1
        self.batch_operation_count += batch_size
        self.record_latency(f"batch_{batch_type}", latency_ms)
        if not success:
            self.record_error(f"batch_{batch_type}")

    def get_latency_stats(self, operation: str) -> dict[str, Any]:
        """Get latency statistics for a specific operation."""
        return self.latencies[operation].get_stats()

    def get_all_stats(self) -> dict[str, Any]:
        """
        Get all metrics.

        Returns:
            Comprehensive metrics dictionary
        """
        elapsed_seconds = time.time() - self.start_time

        # Calculate rates
        total_operations = sum(self.operation_counts.values())
        total_errors = sum(self.error_counts.values())

        # Calculate cache hit rate
        total_cache_accesses = self.cache_hits + self.cache_misses
        cache_hit_rate = (
            self.cache_hits / total_cache_accesses
            if total_cache_accesses > 0
            else 0.0
        )

        # Calculate error rate
        error_rate = (
            total_errors / total_operations
            if total_operations > 0
            else 0.0
        )

        return {
            "uptime_seconds": elapsed_seconds,
            "operations": {
                "total": total_operations,
                "rate_per_sec": total_operations / elapsed_seconds if elapsed_seconds > 0 else 0.0,
                "by_type": dict(self.operation_counts),
            },
            "errors": {
                "total": total_errors,
                "rate": error_rate,
                "by_type": dict(self.error_counts),
            },
            "latencies": {
                operation: tracker.get_stats()
                for operation, tracker in self.latencies.items()
            },
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "hit_rate": cache_hit_rate,
            },
            "connections": {
                "active": self.active_connections,
                "idle": self.idle_connections,
            }
        }

    def get_stats(self) -> dict[str, Any]:
        """
        Get stats in QueryMetrics-compatible format.

        Returns:
            Dictionary with total_queries, avg_latency_ms, error_rate, etc.
        """
        total_operations = sum(self.operation_counts.values())
        total_errors = sum(self.error_counts.values())

        # Calculate overall latency stats
        all_latencies = []
        for tracker in self.latencies.values():
            all_latencies.extend(tracker.samples)

        if all_latencies:
            avg_latency = sum(all_latencies) / len(all_latencies)
            min_latency = min(all_latencies)
            max_latency = max(all_latencies)
        else:
            avg_latency = 0.0
            min_latency = 0.0
            max_latency = 0.0

        return {
            "total_queries": total_operations,
            "total_errors": total_errors,
            "error_rate": total_errors / total_operations if total_operations > 0 else 0.0,
            "avg_latency_ms": avg_latency,
            "min_latency_ms": min_latency,
            "max_latency_ms": max_latency,
            "operations": dict(self.operation_counts),
            "error_types": dict(self.error_types),
        }

    def reset(self):
        """Reset all metrics counters."""
        self.latencies.clear()
        self.operation_counts.clear()
        self.error_counts.clear()
        self.error_types.clear()
        self.cache_hits = 0
        self.cache_misses = 0
        self.batch_count = 0
        self.batch_operation_count = 0
        self.start_time = time.time()

    def export_prometheus(self) -> str:
        """
        Export metrics in Prometheus format.

        Returns:
            Prometheus-formatted metrics string
        """
        lines = []
        stats = self.get_all_stats()

        # Operation counts
        for operation, count in stats["operations"]["by_type"].items():
            lines.append(
                f'scylladb_operations_total{{operation="{operation}"}} {count}'
            )

        # Error counts
        for operation, count in stats["errors"]["by_type"].items():
            lines.append(
                f'scylladb_errors_total{{operation="{operation}"}} {count}'
            )

        # Latencies
        for operation, latency_stats in stats["latencies"].items():
            for percentile_name, value in latency_stats.items():
                if percentile_name.startswith('p'):
                    lines.append(
                        f'scylladb_latency_ms{{'
                        f'operation="{operation}",percentile="{percentile_name}"'
                        f'}} {value}'
                    )

        # Cache metrics
        lines.append(f'scylladb_cache_hits_total {stats["cache"]["hits"]}')
        lines.append(f'scylladb_cache_misses_total {stats["cache"]["misses"]}')
        lines.append(f'scylladb_cache_hit_rate {stats["cache"]["hit_rate"]}')

        return '\n'.join(lines)


# ============================================================================
# Alerting
# ============================================================================

class AlertSeverity(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertManager:
    """
    Alert manager for sending notifications to various channels.

    Supports:
    - PagerDuty for critical alerts
    - Slack for warnings
    - Email for info (future)
    """

    def __init__(
        self,
        pagerduty_key: Optional[str] = None,
        slack_webhook_url: Optional[str] = None,
    ):
        """
        Initialize alert manager.

        Args:
            pagerduty_key: PagerDuty integration key
            slack_webhook_url: Slack webhook URL
        """
        self.pagerduty_key = pagerduty_key
        self.slack_webhook_url = slack_webhook_url
        self.alert_history: list[dict[str, Any]] = []
        self.max_history_size = 100

    async def alert(
        self,
        severity: AlertSeverity,
        message: str,
        details: Optional[dict[str, Any]] = None
    ):
        """
        Send alert to appropriate channel based on severity.

        Args:
            severity: Alert severity
            message: Alert message
            details: Additional context
        """
        if severity == AlertSeverity.CRITICAL and self.pagerduty_key:
            await self._send_pagerduty(message, details)
        elif severity == AlertSeverity.WARNING and self.slack_webhook_url:
            await self._send_slack(message, details)
        else:
            # Fallback to logging
            logger.log(
                logging.CRITICAL if severity == AlertSeverity.CRITICAL else logging.WARNING,
                f"ALERT [{severity}]: {message} - {details}"
            )

    async def _send_pagerduty(self, message: str, details: Optional[dict[str, Any]]):
        """Send alert to PagerDuty."""
        try:
            import aiohttp
        except ImportError:
            logger.error("aiohttp required for PagerDuty alerts. Install with: pip install aiohttp")
            return

        payload = {
            "routing_key": self.pagerduty_key,
            "event_action": "trigger",
            "payload": {
                "summary": message,
                "severity": "critical",
                "source": "scylladb-store",
                "custom_details": details or {}
            }
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://events.pagerduty.com/v2/enqueue",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 202:
                        logger.error(f"PagerDuty alert failed: {response.status}")
        except Exception as e:
            logger.error(f"Failed to send PagerDuty alert: {e}")

    async def _send_slack(self, message: str, details: Optional[dict[str, Any]]):
        """Send alert to Slack."""
        try:
            import aiohttp
        except ImportError:
            logger.error("aiohttp required for Slack alerts. Install with: pip install aiohttp")
            return

        payload = {
            "text": f"⚠️ *ScyllaDB Store Alert*\n{message}",
            "attachments": [
                {
                    "color": "warning",
                    "fields": [
                        {"title": key, "value": str(value), "short": True}
                        for key, value in (details or {}).items()
                    ]
                }
            ]
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.slack_webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        logger.error(f"Slack alert failed: {response.status}")
        except Exception as e:
            logger.error(f"Failed to send Slack alert: {e}")

    def trigger_alert(
        self,
        severity: AlertSeverity,
        message: str,
        context: Optional[dict[str, Any]] = None
    ):
        """
        Trigger an alert synchronously (stores in history, logs immediately).

        Args:
            severity: Alert severity
            message: Alert message
            context: Additional context
        """
        import time
        from datetime import datetime

        # Store in history
        alert_record = {
            "severity": severity.value,
            "message": message,
            "context": context or {},
            "timestamp": datetime.utcnow().isoformat()
        }

        self.alert_history.append(alert_record)

        # Keep history bounded
        if len(self.alert_history) > self.max_history_size:
            self.alert_history = self.alert_history[-self.max_history_size:]

        # Log immediately
        logger.log(
            logging.CRITICAL if severity == AlertSeverity.CRITICAL else logging.WARNING,
            f"ALERT [{severity.value}]: {message} - {context}"
        )

        # Note: For async sending to PagerDuty/Slack, use the alert() method instead

    def get_recent_alerts(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get recent alerts from history.

        Args:
            limit: Maximum number of alerts to return

        Returns:
            List of recent alerts (most recent first)
        """
        return list(reversed(self.alert_history[-limit:]))

    def clear_history(self):
        """Clear alert history."""
        self.alert_history.clear()


# ============================================================================
# Convenience Functions
# ============================================================================

# Global tracer instance (can be configured)
_global_tracer: Optional[Tracer] = None

def get_tracer(service_name: str = "scylladb-store") -> Tracer:
    """Get or create global tracer instance."""
    global _global_tracer
    if _global_tracer is None:
        _global_tracer = Tracer(service_name=service_name)
    return _global_tracer


def configure_tracing(service_name: str, enabled: bool = True):
    """Configure global tracing."""
    global _global_tracer
    _global_tracer = Tracer(service_name=service_name, enabled=enabled)
