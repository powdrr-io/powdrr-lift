"""Small retry policy for transient HTTP throttling and server failures."""

from __future__ import annotations

import time
from collections.abc import Callable


def retry_transient_http[ResultT](
    operation: Callable[[], ResultT],
    *,
    max_retries: int = 3,
    base_delay_seconds: float = 1.0,
) -> ResultT:
    """Retry HTTP 429 and 5xx exceptions with bounded exponential backoff."""
    retries = 0
    while True:
        try:
            return operation()
        except Exception as error:
            status_code = getattr(error, "status_code", getattr(error, "code", None))
            retryable = isinstance(status_code, int) and (
                status_code == 429 or 500 <= status_code <= 599
            )
            if not retryable or retries >= max(0, max_retries):
                raise
            time.sleep(base_delay_seconds * (2**retries))
            retries += 1
