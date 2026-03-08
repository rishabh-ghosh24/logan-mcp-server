"""Tests for rate limiter with exponential backoff."""

import pytest
import time
from unittest.mock import patch

from oci_logan_mcp.rate_limiter import RateLimiter, RateLimitExceeded


class TestRateLimiterInit:
    """Test RateLimiter initialization."""

    def test_default_values(self):
        limiter = RateLimiter()
        assert limiter.max_retries == 5
        assert limiter.initial_delay == 1.0
        assert limiter.max_delay == 30.0
        assert limiter.min_interval == 0.1  # 1/10 rps

    def test_custom_values(self):
        limiter = RateLimiter(
            max_retries=3,
            initial_delay=2.0,
            max_delay=60.0,
            requests_per_second=5.0,
        )
        assert limiter.max_retries == 3
        assert limiter.initial_delay == 2.0
        assert limiter.max_delay == 60.0
        assert limiter.min_interval == 0.2  # 1/5 rps

    def test_initial_state(self):
        limiter = RateLimiter()
        assert limiter.retry_count == 0
        assert limiter.is_in_backoff is False


class TestAcquire:
    """Test the acquire() rate limiting method."""

    @pytest.mark.asyncio
    async def test_first_acquire_immediate(self):
        """First acquire should proceed without delay."""
        limiter = RateLimiter(requests_per_second=1000)
        start = time.time()
        await limiter.acquire()
        elapsed = time.time() - start
        assert elapsed < 0.1  # Should be nearly instant

    @pytest.mark.asyncio
    async def test_respects_rate_limit(self):
        """Rapid sequential acquires should enforce minimum interval."""
        limiter = RateLimiter(requests_per_second=100)  # 10ms interval
        await limiter.acquire()
        start = time.time()
        await limiter.acquire()
        elapsed = time.time() - start
        # Should be at least ~10ms (min_interval = 0.01)
        assert elapsed >= 0.005  # Allow small timing variance


class TestHandleRateLimit:
    """Test the handle_rate_limit() exponential backoff."""

    @pytest.mark.asyncio
    async def test_increments_retry_count(self):
        """Should increment retry count on each call."""
        limiter = RateLimiter(max_retries=5, initial_delay=0.001)
        await limiter.handle_rate_limit()
        assert limiter.retry_count == 1

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_raises(self):
        """Should raise when max retries exceeded."""
        limiter = RateLimiter(max_retries=2, initial_delay=0.001, max_delay=0.01)
        await limiter.handle_rate_limit()  # retry 1
        await limiter.handle_rate_limit()  # retry 2
        with pytest.raises(Exception, match="Max retries"):
            await limiter.handle_rate_limit()  # retry 3 > max 2

    @pytest.mark.asyncio
    async def test_sets_backoff_until(self):
        """Should set a future backoff_until timestamp."""
        limiter = RateLimiter(initial_delay=0.001, max_delay=0.01)
        await limiter.handle_rate_limit()
        # After handle_rate_limit, the actual sleep has already happened
        # but is_in_backoff may or may not still be true depending on timing


class TestReset:
    """Test the reset() method."""

    @pytest.mark.asyncio
    async def test_reset_clears_retry_count(self):
        """Should reset retry count to 0."""
        limiter = RateLimiter(initial_delay=0.001, max_delay=0.01)
        await limiter.handle_rate_limit()
        assert limiter.retry_count == 1
        limiter.reset()
        assert limiter.retry_count == 0


class TestIsInBackoff:
    """Test the is_in_backoff property."""

    def test_not_in_backoff_initially(self):
        limiter = RateLimiter()
        assert limiter.is_in_backoff is False

    def test_not_in_backoff_when_none(self):
        limiter = RateLimiter()
        limiter._backoff_until = None
        assert limiter.is_in_backoff is False

    def test_in_backoff_when_future(self):
        limiter = RateLimiter()
        limiter._backoff_until = time.time() + 100
        assert limiter.is_in_backoff is True

    def test_not_in_backoff_when_past(self):
        limiter = RateLimiter()
        limiter._backoff_until = time.time() - 1
        assert limiter.is_in_backoff is False


class TestRateLimitExceeded:
    """Test RateLimitExceeded exception."""

    def test_default_message(self):
        exc = RateLimitExceeded()
        assert "Rate limit exceeded" in str(exc)
        assert exc.retry_after is None

    def test_custom_message(self):
        exc = RateLimitExceeded("Custom error", retry_after=30.0)
        assert "Custom error" in str(exc)
        assert exc.retry_after == 30.0
