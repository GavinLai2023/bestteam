from __future__ import annotations

import time
from typing import Any, Callable, Tuple, Type


def with_retry(
    fn: Callable[[], Any],
    *,
    retries: int = 3,
    backoff: float = 1.0,
    retriable_exc: Tuple[Type[BaseException], ...] = (Exception,),
) -> Any:
    """Call fn up to `retries` times, sleeping between failures.

    Only retries on exceptions listed in retriable_exc. Re-raises the last
    exception when all attempts are exhausted.
    """
    for attempt in range(retries):
        try:
            return fn()
        except retriable_exc as exc:
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
