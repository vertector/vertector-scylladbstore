"""
Rate limiting implementation for AsyncScyllaDBStore.

Provides:
- Token bucket rate limiter
- Sliding window rate limiter
- Per-operation rate limits
- Decorator for easy integration
"""

import time
import asyncio
import logging
from typing import Optional, Callable
from functools import wraps
from collections import defaultdict, deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ============================================================================
# Rate Limiter Exceptions
# ============================================================================

class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str, retry_after: float):
        """
        Initialize rate limit exception.

        Args:
            message: Error message
            retry_after: Seconds to wait before retrying
        """
        super().__init__(message)
        self.retry_after = retry_after


# ============================================================================
# Token Bucket Rate Limiter
# ============================================================================

@dataclass
class TokenBucket:
    """
    Token bucket for rate limiting.

    Allows burst traffic up to capacity, then limits to fill_rate.
    """

    capacity: int  # Maximum tokens (burst size)
    fill_rate: float  # Tokens per second
    tokens: float = 0.0  # Current tokens
    last_update: float = 0.0  # Last update time

    def __post_init__(self):
        """Initialize token bucket."""
        self.tokens = float(self.capacity)
        self.last_update = time.monotonic()

    def consume(self, tokens: int = 1) -> bool:
        """
        Attempt to consume tokens.

        Args:
            tokens: Number of tokens to consume

        Returns:
            True if tokens consumed successfully, False otherwise
        """
        now = time.monotonic()

        # Add tokens based on elapsed time
        elapsed = now - self.last_update
        self.tokens = min(
            self.capacity,
            self.tokens + (elapsed * self.fill_rate)
        )
        self.last_update = now

        # Try to consume
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True

        return False

    def get_wait_time(self, tokens: int = 1) -> float:
        """
        Get time to wait before tokens available.

        Args:
            tokens: Number of tokens needed

        Returns:
            Seconds to wait
        """
        if self.tokens >= tokens:
            return 0.0

        tokens_needed = tokens - self.tokens
        return tokens_needed / self.fill_rate


class TokenBucketRateLimiter:
    """
    Token bucket-based rate limiter.

    Supports per-operation rate limits with burst capacity.
    """

    def __init__(
        self,
        default_requests_per_second: int = 1000,
        default_burst_size: int = 100,
    ):
        """
        Initialize rate limiter.

        Args:
            default_requests_per_second: Default rate limit (requests per second)
            default_burst_size: Default burst capacity
        """
        self.default_requests_per_second = default_requests_per_second
        self.default_burst_size = default_burst_size

        # Per-operation buckets
        self.buckets: dict[str, TokenBucket] = {}

        # Operation-specific limits
        self.operation_limits: dict[str, tuple[int, int]] = {}

    def configure_operation(
        self,
        operation: str,
        requests_per_second: int,
        burst_size: int
    ):
        """
        Configure rate limit for specific operation.

        Args:
            operation: Operation name (e.g., "put", "search")
            requests_per_second: Rate limit (requests per second)
            burst_size: Burst capacity
        """
        self.operation_limits[operation] = (requests_per_second, burst_size)

    def _get_bucket(self, operation: str) -> TokenBucket:
        """Get or create token bucket for operation."""
        if operation not in self.buckets:
            # Get operation-specific limits or use defaults
            if operation in self.operation_limits:
                rps, burst = self.operation_limits[operation]
            else:
                rps, burst = self.default_requests_per_second, self.default_burst_size

            # Use requests per second as the fill rate
            fill_rate = rps

            self.buckets[operation] = TokenBucket(
                capacity=burst,
                fill_rate=fill_rate
            )

        return self.buckets[operation]

    async def acquire(self, operation: str, tokens: int = 1):
        """
        Acquire tokens for an operation (async with backoff).

        Args:
            operation: Operation name
            tokens: Number of tokens to acquire

        Raises:
            RateLimitExceeded: If rate limit exceeded and wait time > threshold
        """
        bucket = self._get_bucket(operation)

        while not bucket.consume(tokens):
            wait_time = bucket.get_wait_time(tokens)

            # If wait time is too long, raise exception
            if wait_time > 10.0:  # 10 second threshold
                raise RateLimitExceeded(
                    f"Rate limit exceeded for operation '{operation}'. "
                    f"Retry after {wait_time:.1f} seconds.",
                    retry_after=wait_time
                )

            # Otherwise, wait and retry
            logger.warning(
                f"Rate limit approached for '{operation}'. "
                f"Waiting {wait_time:.2f}s..."
            )
            await asyncio.sleep(wait_time)

    def check(self, operation: str, tokens: int = 1) -> bool:
        """
        Check if tokens available without blocking.

        Args:
            operation: Operation name
            tokens: Number of tokens to check

        Returns:
            True if tokens available, False otherwise
        """
        bucket = self._get_bucket(operation)
        return bucket.consume(tokens)

    def get_retry_after(self, operation: str, tokens: int = 1) -> float:
        """
        Get time to wait before retrying operation.

        Args:
            operation: Operation name
            tokens: Number of tokens needed

        Returns:
            Seconds to wait before retrying
        """
        bucket = self._get_bucket(operation)
        return bucket.get_wait_time(tokens)

    def get_stats(self) -> dict[str, dict]:
        """
        Get rate limiter statistics.

        Returns:
            Dictionary with per-operation stats
        """
        stats = {}
        for operation, bucket in self.buckets.items():
            stats[operation] = {
                "capacity": bucket.capacity,
                "fill_rate": bucket.fill_rate,
                "available_tokens": bucket.tokens,
                "utilization": 1.0 - (bucket.tokens / bucket.capacity)
            }
        return stats


# ============================================================================
# Sliding Window Rate Limiter
# ============================================================================

class SlidingWindowRateLimiter:
    """
    Sliding window rate limiter.

    More accurate than token bucket for strict rate limits.
    """

    def __init__(
        self,
        requests_per_minute: int = 1000,
        window_size_seconds: int = 60
    ):
        """
        Initialize sliding window rate limiter.

        Args:
            requests_per_minute: Maximum requests per minute
            window_size_seconds: Window size in seconds
        """
        self.requests_per_minute = requests_per_minute
        self.window_size = window_size_seconds

        # Request timestamps per operation
        self.windows: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=requests_per_minute * 2)
        )

    def _clean_window(self, operation: str):
        """Remove timestamps outside the window."""
        now = time.monotonic()
        cutoff = now - self.window_size

        window = self.windows[operation]

        # Remove old timestamps
        while window and window[0] < cutoff:
            window.popleft()

    def check(self, operation: str) -> bool:
        """
        Check if request allowed without blocking.

        Args:
            operation: Operation name

        Returns:
            True if allowed, False if rate limited
        """
        self._clean_window(operation)

        window = self.windows[operation]

        if len(window) < self.requests_per_minute:
            window.append(time.monotonic())
            return True

        return False

    async def acquire(self, operation: str):
        """
        Acquire permission for an operation (async with backoff).

        Args:
            operation: Operation name

        Raises:
            RateLimitExceeded: If rate limit exceeded
        """
        self._clean_window(operation)

        window = self.windows[operation]

        if len(window) < self.requests_per_minute:
            window.append(time.monotonic())
            return

        # Calculate wait time
        oldest_timestamp = window[0]
        wait_time = self.window_size - (time.monotonic() - oldest_timestamp)

        if wait_time > 0:
            if wait_time > 10.0:
                raise RateLimitExceeded(
                    f"Rate limit exceeded for operation '{operation}'. "
                    f"Retry after {wait_time:.1f} seconds.",
                    retry_after=wait_time
                )

            logger.warning(
                f"Rate limit approached for '{operation}'. "
                f"Waiting {wait_time:.2f}s..."
            )
            await asyncio.sleep(wait_time)

        window.append(time.monotonic())

    def get_stats(self) -> dict[str, dict]:
        """Get rate limiter statistics."""
        stats = {}
        for operation, window in self.windows.items():
            self._clean_window(operation)
            stats[operation] = {
                "requests_in_window": len(window),
                "limit": self.requests_per_minute,
                "utilization": len(window) / self.requests_per_minute
            }
        return stats


# ============================================================================
# Decorator for Rate Limiting
# ============================================================================

def rate_limit(
    limiter: TokenBucketRateLimiter | SlidingWindowRateLimiter,
    operation: Optional[str] = None,
    tokens: int = 1
):
    """
    Decorator to rate limit async functions.

    Usage:
        limiter = TokenBucketRateLimiter()

        @rate_limit(limiter, operation="put")
        async def aput(self, namespace, key, value):
            ...

    Args:
        limiter: Rate limiter instance
        operation: Operation name (defaults to function name)
        tokens: Number of tokens to consume
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            op_name = operation or func.__name__

            # Acquire rate limit
            await limiter.acquire(op_name, tokens)

            # Execute function
            return await func(*args, **kwargs)

        return wrapper
    return decorator


# ============================================================================
# Composite Rate Limiter
# ============================================================================

class CompositeRateLimiter:
    """
    Composite rate limiter supporting multiple strategies.

    Combines token bucket (burst) and sliding window (strict).
    """

    def __init__(
        self,
        requests_per_minute: int = 1000,
        burst_size: int = 100,
        use_sliding_window: bool = False
    ):
        """
        Initialize composite rate limiter.

        Args:
            requests_per_minute: Rate limit
            burst_size: Burst capacity
            use_sliding_window: Use sliding window instead of token bucket
        """
        if use_sliding_window:
            self.limiter = SlidingWindowRateLimiter(
                requests_per_minute=requests_per_minute
            )
        else:
            self.limiter = TokenBucketRateLimiter(
                default_requests_per_minute=requests_per_minute,
                default_burst_size=burst_size
            )

    async def acquire(self, operation: str, tokens: int = 1):
        """Acquire rate limit."""
        await self.limiter.acquire(operation, tokens)

    def check(self, operation: str, tokens: int = 1) -> bool:
        """Check rate limit without blocking."""
        return self.limiter.check(operation, tokens)

    def get_stats(self) -> dict:
        """Get rate limiter statistics."""
        return self.limiter.get_stats()

    def configure_operation(
        self,
        operation: str,
        requests_per_minute: int,
        burst_size: int = None
    ):
        """Configure operation-specific rate limit."""
        if isinstance(self.limiter, TokenBucketRateLimiter):
            burst = burst_size or (requests_per_minute // 10)
            self.limiter.configure_operation(operation, requests_per_minute, burst)
