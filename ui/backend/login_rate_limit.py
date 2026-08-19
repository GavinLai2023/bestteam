"""Login throttling: a sliding window over failed attempts (beta gate G2).

Two keys, both counted only on a *failed* login: the username (lower-cased,
so `Alice` and `alice` share a budget) and the client address. Once a key has
`limit` failures inside `window_seconds`, `/api/auth/login` answers 429 with
`Retry-After` **before** it hashes anything -- PBKDF2 at 260k iterations is
~0.76 s of CPU per attempt, so unthrottled guessing is a self-inflicted
denial of service as much as a credential attack. A successful login clears
the username's failures (the legitimate owner is back) but not the address's
(the address may be shared with the attacker).

Deliberately in-process and in-memory: the deployment is one process (see
`docs/DECISIONS.md`), a restart forgiving the counters is acceptable, and a
table would need its own sweep. Behind a reverse proxy the address uvicorn
reports is the proxy's unless `--forwarded-allow-ips` trusts it (see
`docs/deployment.md`); the username key still holds either way, which is why
there are two.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional


class LoginRateLimiter:
    def __init__(
        self,
        *,
        username_limit: int = 5,
        ip_limit: int = 20,
        window_seconds: int = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.username_limit = username_limit
        self.ip_limit = ip_limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._failures: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _keys(username: str, ip: Optional[str]):
        yield f"user:{username.strip().lower()}"
        if ip:
            yield f"ip:{ip}"

    def _limit_for(self, key: str) -> int:
        return self.username_limit if key.startswith("user:") else self.ip_limit

    def _prune(self, key: str, now: float) -> Optional[Deque[float]]:
        window = self._failures.get(key)
        if window is None:
            return None
        cutoff = now - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if not window:
            del self._failures[key]
            return None
        return window

    def retry_after(self, username: str, ip: Optional[str]) -> Optional[int]:
        """Seconds until the next attempt is allowed, or `None` if it is now."""
        now = self._clock()
        with self._lock:
            worst = 0.0
            for key in self._keys(username, ip):
                window = self._prune(key, now)
                if window is not None and len(window) >= self._limit_for(key):
                    worst = max(worst, window[0] + self.window_seconds - now)
        if worst <= 0:
            return None
        return max(1, int(-(-worst // 1)))  # ceil

    def record_failure(self, username: str, ip: Optional[str]) -> None:
        now = self._clock()
        with self._lock:
            for key in self._keys(username, ip):
                self._failures.setdefault(key, deque()).append(now)
            # A guesser rotating usernames grows the dict by one key per
            # name, so sweep expired keys here. Cheap by construction: a
            # failure is only recorded after PBKDF2 ran (~0.76 s of CPU), so
            # the dict cannot grow faster than a few keys per second and is
            # empty again one window after the guessing stops.
            for key in list(self._failures):
                self._prune(key, now)

    def record_success(self, username: str) -> None:
        with self._lock:
            self._failures.pop(next(self._keys(username, None)), None)

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._failures)
