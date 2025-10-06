"""
Tests for rate_limiter module (token bucket, sliding window).
"""

import pytest
import asyncio
import time

from vertector_scylladbstore.rate_limiter import (
    TokenBucket,
    TokenBucketRateLimiter,
    SlidingWindowRateLimiter,
    RateLimitExceeded,
    rate_limit,
)


# ============================================================================
# TokenBucket Tests
# ============================================================================

class TestTokenBucket:
    """Test token bucket implementation."""

    def test_bucket_initialization(self):
        """Test token bucket initializes with full capacity."""
        bucket = TokenBucket(capacity=100, fill_rate=10.0)

        assert bucket.capacity == 100
        assert bucket.fill_rate == 10.0
        assert bucket.tokens == 100.0

    def test_consume_tokens_success(self):
        """Test consuming tokens when available."""
        bucket = TokenBucket(capacity=100, fill_rate=10.0)

        # Consume 50 tokens
        result = bucket.consume(50)

        assert result == True
        assert bucket.tokens == 50.0

    def test_consume_tokens_failure(self):
        """Test consuming tokens when insufficient."""
        bucket = TokenBucket(capacity=100, fill_rate=10.0)

        # Consume all tokens
        bucket.consume(100)

        # Try to consume more
        result = bucket.consume(10)

        assert result == False
        assert bucket.tokens < 1.0  # May have tiny refill due to time passage

    def test_tokens_refill_over_time(self):
        """Test tokens refill based on fill_rate."""
        bucket = TokenBucket(capacity=100, fill_rate=10.0)  # 10 tokens/sec

        # Consume all tokens
        bucket.consume(100)
        assert bucket.tokens == 0.0

        # Wait 0.5 seconds (should refill 5 tokens)
        time.sleep(0.5)

        # Try to consume 5 tokens
        result = bucket.consume(5)
        assert result == True

    def test_tokens_capped_at_capacity(self):
        """Test tokens don't exceed capacity."""
        bucket = TokenBucket(capacity=100, fill_rate=10.0)

        # Consume 50 tokens
        bucket.consume(50)

        # Wait 10 seconds (would add 100 tokens, but capped at capacity)
        time.sleep(1.0)

        # Should have 100 tokens max, not 150
        assert bucket.tokens <= 100.0

    def test_get_wait_time_zero_when_available(self):
        """Test wait time is zero when tokens available."""
        bucket = TokenBucket(capacity=100, fill_rate=10.0)

        wait_time = bucket.get_wait_time(50)
        assert wait_time == 0.0

    def test_get_wait_time_calculation(self):
        """Test wait time calculation when tokens needed."""
        bucket = TokenBucket(capacity=100, fill_rate=10.0)  # 10 tokens/sec

        # Consume all tokens
        bucket.consume(100)

        # Need 20 tokens = 2 seconds wait (20 / 10)
        wait_time = bucket.get_wait_time(20)
        assert wait_time == pytest.approx(2.0, abs=0.1)


# ============================================================================
# TokenBucketRateLimiter Tests
# ============================================================================

class TestTokenBucketRateLimiter:
    """Test token bucket rate limiter."""

    def test_rate_limiter_initialization(self):
        """Test rate limiter initializes with defaults."""
        limiter = TokenBucketRateLimiter(
            default_requests_per_second=1000,
            default_burst_size=100
        )

        assert limiter.default_requests_per_second == 1000
        assert limiter.default_burst_size == 100
        assert len(limiter.buckets) == 0

    def test_configure_operation(self):
        """Test configuring per-operation limits."""
        limiter = TokenBucketRateLimiter()

        limiter.configure_operation("expensive_op", requests_per_second=10, burst_size=5)

        assert "expensive_op" in limiter.operation_limits
        assert limiter.operation_limits["expensive_op"] == (10, 5)

    def test_check_allows_request(self):
        """Test check() allows requests within limit."""
        limiter = TokenBucketRateLimiter(
            default_requests_per_second=10,
            default_burst_size=10
        )

        # First request should succeed
        result = limiter.check("test_op")
        assert result == True

    def test_check_blocks_when_exhausted(self):
        """Test check() blocks when tokens exhausted."""
        limiter = TokenBucketRateLimiter(
            default_requests_per_second=10,
            default_burst_size=5
        )

        # Exhaust tokens
        for _ in range(5):
            limiter.check("test_op")

        # Next request should fail
        result = limiter.check("test_op")
        assert result == False

    @pytest.mark.asyncio
    async def test_acquire_waits_and_succeeds(self):
        """Test acquire() waits for tokens to become available."""
        limiter = TokenBucketRateLimiter(
            default_requests_per_second=10,  # 10/sec
            default_burst_size=2
        )

        # Consume initial burst
        await limiter.acquire("test_op", tokens=2)

        # Next acquire should wait ~0.1s for 1 token
        start = time.perf_counter()
        await limiter.acquire("test_op", tokens=1)
        elapsed = time.perf_counter() - start

        assert elapsed >= 0.05  # Should have waited

    @pytest.mark.asyncio
    async def test_acquire_raises_on_long_wait(self):
        """Test acquire() raises exception for long waits."""
        limiter = TokenBucketRateLimiter(
            default_requests_per_second=1,  # Very slow refill
            default_burst_size=1
        )

        # Consume token
        await limiter.acquire("test_op")

        # Next request needs 20 tokens (20 second wait > 10s threshold)
        with pytest.raises(RateLimitExceeded) as exc_info:
            await limiter.acquire("test_op", tokens=20)

        assert exc_info.value.retry_after > 10.0

    def test_get_retry_after(self):
        """Test get_retry_after() returns correct wait time."""
        limiter = TokenBucketRateLimiter(
            default_requests_per_second=10,
            default_burst_size=10
        )

        # Exhaust tokens
        limiter.check("test_op", tokens=10)

        # Need 5 more tokens = 0.5 seconds
        retry_after = limiter.get_retry_after("test_op", tokens=5)
        assert retry_after == pytest.approx(0.5, abs=0.1)

    def test_get_stats(self):
        """Test get_stats() returns bucket statistics."""
        limiter = TokenBucketRateLimiter(
            default_requests_per_second=100,
            default_burst_size=50
        )

        # Use some tokens
        limiter.check("op1", tokens=10)
        limiter.check("op2", tokens=20)

        stats = limiter.get_stats()

        assert "op1" in stats
        assert "op2" in stats
        assert stats["op1"]["capacity"] == 50
        assert stats["op1"]["fill_rate"] == 100
        assert "utilization" in stats["op1"]


# ============================================================================
# SlidingWindowRateLimiter Tests
# ============================================================================

class TestSlidingWindowRateLimiter:
    """Test sliding window rate limiter."""

    def test_sliding_window_initialization(self):
        """Test sliding window initializes correctly."""
        limiter = SlidingWindowRateLimiter(
            requests_per_minute=60,
            window_size_seconds=60
        )

        assert limiter.requests_per_minute == 60
        assert limiter.window_size == 60

    def test_check_allows_within_limit(self):
        """Test check() allows requests within limit."""
        limiter = SlidingWindowRateLimiter(
            requests_per_minute=10,
            window_size_seconds=60
        )

        # First 10 requests should succeed
        for _ in range(10):
            result = limiter.check("test_op")
            assert result == True

        # 11th request should fail
        result = limiter.check("test_op")
        assert result == False

    def test_window_slides_over_time(self):
        """Test old requests expire from window."""
        limiter = SlidingWindowRateLimiter(
            requests_per_minute=5,
            window_size_seconds=1  # 1 second window
        )

        # Make 5 requests
        for _ in range(5):
            limiter.check("test_op")

        # Should be at limit
        assert limiter.check("test_op") == False

        # Wait for window to slide
        time.sleep(1.1)

        # Should be able to make requests again
        assert limiter.check("test_op") == True

    @pytest.mark.asyncio
    async def test_acquire_waits_for_window(self):
        """Test acquire() waits for window to slide."""
        limiter = SlidingWindowRateLimiter(
            requests_per_minute=2,
            window_size_seconds=1
        )

        # Fill window
        await limiter.acquire("test_op")
        await limiter.acquire("test_op")

        # Next acquire should wait ~1 second
        start = time.perf_counter()
        await limiter.acquire("test_op")
        elapsed = time.perf_counter() - start

        assert elapsed >= 0.8  # Should have waited

    def test_get_stats(self):
        """Test get_stats() returns window statistics."""
        limiter = SlidingWindowRateLimiter(
            requests_per_minute=100
        )

        # Make some requests
        for _ in range(30):
            limiter.check("test_op")

        stats = limiter.get_stats()

        assert "test_op" in stats
        assert stats["test_op"]["requests_in_window"] == 30
        assert stats["test_op"]["limit"] == 100
        assert stats["test_op"]["utilization"] == pytest.approx(0.3, abs=0.01)


# ============================================================================
# Decorator Tests
# ============================================================================

class TestRateLimitDecorator:
    """Test rate_limit decorator."""

    @pytest.mark.asyncio
    async def test_decorator_applies_rate_limiting(self):
        """Test decorator applies rate limiting to function."""
        limiter = TokenBucketRateLimiter(
            default_requests_per_second=10,
            default_burst_size=2
        )

        call_count = 0

        @rate_limit(limiter, operation="decorated_op", tokens=1)
        async def test_function():
            nonlocal call_count
            call_count += 1
            return "success"

        # First 2 calls should succeed immediately
        result1 = await test_function()
        result2 = await test_function()

        assert result1 == "success"
        assert result2 == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_decorator_uses_function_name_as_operation(self):
        """Test decorator uses function name when operation not specified."""
        limiter = TokenBucketRateLimiter(
            default_requests_per_second=100,
            default_burst_size=10
        )

        @rate_limit(limiter)  # No operation specified
        async def my_custom_function():
            return "done"

        await my_custom_function()

        # Should have used function name
        stats = limiter.get_stats()
        assert "my_custom_function" in stats
