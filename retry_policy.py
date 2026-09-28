"""Bounded retry policy for transient external API failures."""
from __future__ import annotations

import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

DEFAULT_MAX_RETRIES = 2
DEFAULT_INITIAL_DELAY = 1.0
DEFAULT_MAX_DELAY = 4.0


def extract_status_code(error: BaseException) -> Optional[int]:
    for attr in ("status_code", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def is_transient_error(error: BaseException) -> bool:
    status = extract_status_code(error)
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    text = str(error).lower()
    transient_markers = (
        "timeout", "timed out", "temporarily unavailable", "service unavailable",
        "connection reset", "connection aborted", "connection refused",
        "temporary failure", "high demand", "resource exhausted",
    )
    return any(marker in text for marker in transient_markers)


def call_with_retry(
    operation: Callable[[], T],
    *,
    operation_name: str,
    logger,
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> T:
    """Retry only transient failures, with bounded exponential backoff."""
    retries = max(0, int(max_retries))
    delay = max(0.0, float(initial_delay))
    ceiling = max(delay, float(max_delay))

    for attempt in range(retries + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= retries or not is_transient_error(exc):
                raise
            logger.warning(
                "Transient API failure; retrying | operation=%s attempt=%s/%s delay=%.1fs | %s",
                operation_name, attempt + 1, retries, delay, exc,
            )
            if delay > 0:
                time.sleep(delay)
            delay = min(ceiling, max(0.1, delay * 2))

    raise RuntimeError("Retry loop exited unexpectedly")
