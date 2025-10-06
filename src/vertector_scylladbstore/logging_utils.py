"""
Structured logging utilities for production environments.

Provides:
- Structured JSON logging
- Request ID tracking
- Performance logging
- Error context
"""

import json
import logging
import time
from typing import Any
from contextvars import ContextVar
from functools import wraps

# Context variable for request/operation tracking
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
operation_var: ContextVar[str] = ContextVar("operation", default="")


class StructuredFormatter(logging.Formatter):
    """
    JSON formatter for structured logging.

    Outputs logs in JSON format with consistent fields:
    - timestamp
    - level
    - message
    - request_id (if available)
    - operation (if available)
    - extra fields
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add request ID if available
        request_id = request_id_var.get()
        if request_id:
            log_data["request_id"] = request_id

        # Add operation if available
        operation = operation_var.get()
        if operation:
            log_data["operation"] = operation

        # Add extra fields from record
        if hasattr(record, "extra"):
            log_data.update(record.extra)

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


class PerformanceLogger:
    """
    Performance logging context manager.

    Automatically logs operation duration and outcome.

    Example:
        async with PerformanceLogger("database_query", logger=logger, query="SELECT ..."):
            result = await execute_query()
    """

    def __init__(
        self,
        operation: str,
        logger: logging.Logger,
        **context: Any
    ):
        """
        Initialize performance logger.

        Args:
            operation: Operation name
            logger: Logger instance
            **context: Additional context fields
        """
        self.operation = operation
        self.logger = logger
        self.context = context
        self.start_time = None

    async def __aenter__(self):
        """Start timing."""
        self.start_time = time.perf_counter()
        operation_var.set(self.operation)

        self.logger.info(
            f"Starting operation: {self.operation}",
            extra={"event": "operation_start", **self.context}
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Log duration and outcome."""
        duration_ms = (time.perf_counter() - self.start_time) * 1000

        if exc_type:
            # Operation failed
            self.logger.error(
                f"Operation failed: {self.operation}",
                extra={
                    "event": "operation_failed",
                    "duration_ms": round(duration_ms, 2),
                    "error_type": exc_type.__name__,
                    "error": str(exc_val),
                    **self.context
                }
            )
        else:
            # Operation succeeded
            self.logger.info(
                f"Operation completed: {self.operation}",
                extra={
                    "event": "operation_completed",
                    "duration_ms": round(duration_ms, 2),
                    **self.context
                }
            )

        operation_var.set("")


def setup_production_logging(
    level: str = "INFO",
    format: str = "json"
) -> None:
    """
    Setup production-ready logging configuration.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format: Format type ("json" or "text")

    Example:
        setup_production_logging(level="INFO", format="json")
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create console handler
    handler = logging.StreamHandler()

    if format.lower() == "json":
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
        )

    root_logger.addHandler(handler)


def log_with_context(logger: logging.Logger, level: str, message: str, **context):
    """
    Log message with additional context fields.

    Args:
        logger: Logger instance
        level: Log level
        message: Log message
        **context: Additional context fields
    """
    log_func = getattr(logger, level.lower())
    log_func(message, extra=context)
