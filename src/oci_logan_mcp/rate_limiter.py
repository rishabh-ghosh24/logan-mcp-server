"""Rate limiting with exponential backoff for OCI API calls."""

import asyncio
import time
from typing import Optional


class RateLimiter:
    """Handles rate limiting with exponential backoff."""

    def __init__(
        self,
        max_retries: int = 5,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        requests_per_second: float = 10.0,
    ):
        """Initialize rate limiter.

        Args:
            max_retries: Maximum number of retry attempts on rate limit.
            initial_delay: Initial backoff delay in seconds.
            max_delay: Maximum backoff delay in seconds.
            requests_per_second: Target requests per second limit.
        """
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.min_interval = 1.0 / requests_per_second

        self._last_request_time: float = 0
        self._retry_count: int = 0
        self._backoff_until: Optional[float] = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire permission to make a request."""
        async with self._lock:
            if self._backoff_until:
                wait_time = self._backoff_until - time.time()
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                self._backoff_until = None

            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)

            self._last_request_time = time.time()

    async def handle_rate_limit(self) -> None:
        """Handle a rate limit response (HTTP 429).

        Implements exponential backoff with jitter.

        Raises:
            Exception: If max retries exceeded.
        """
        self._retry_count += 1

        if self._retry_count > self.max_retries:
            self._retry_count = 0
            raise Exception(
                f"Max retries ({self.max_retries}) exceeded due to rate limiting. "
                "Please try again later."
            )

        delay = min(self.initial_delay * (2 ** (self._retry_count - 1)), self.max_delay)

        import random
        jitter = random.uniform(0, delay * 0.1)
        delay += jitter

        self._backoff_until = time.time() + delay
        await asyncio.sleep(delay)

    def reset(self) -> None:
        """Reset retry count after successful request."""
        self._retry_count = 0

    @property
    def is_in_backoff(self) -> bool:
        """Check if currently in backoff period."""
        if self._backoff_until is None:
            return False
        return time.time() < self._backoff_until

    @property
    def retry_count(self) -> int:
        """Get current retry count."""
        return self._retry_count


class RateLimitExceeded(Exception):
    """Exception raised when rate limit is exceeded and retries exhausted."""

    def __init__(self, message: str = "Rate limit exceeded", retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after
