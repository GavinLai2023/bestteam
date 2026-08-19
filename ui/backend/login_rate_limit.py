"""Login throttling: a sliding window over failed attempts (beta gate G2).

Two keys, both counted only on a *failed* login: the username (lower-cased,
so `Alice` and `alice` share a budget) and the client address. Once a key has
`limit` failures inside `window_seconds`, `/api/auth/login` answers 429 with
`Retry-After` **before** it hashes anything -- PBKDF2 at 260k iterations is
~0.76 s of CPU per attempt, so unthrottled guessing is a self-inflicted
denial of service as much as a credential attack. A successful login clears
the username's failures (the legitimate owner is back) but not the address's
(the address may be shared with the attacker).

The check *reserves* the attempt: `reserve` counts it as a failure in the same
locked step that admits it, and only `record_success` takes that back. A
check-then-record pair would let a concurrent burst -- the route runs in
FastAPI's thread pool -- pass the check many times before the first failure
was recorded, hashing (and guessing) once per thread instead of once per slot.

Deliberately in-process and in-memory: the deployment is one process (see
`docs/DECISIONS.md`), a restart forgiving the counters is acceptable, and a
table would need its own sweep. Behind a reverse proxy the address uvicorn
reports is the proxy's unless `--forwarded-allow-ips` trusts it (see
`docs/deployment.md`); the username key still holds either way, which is why
there are two.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

# The full sweep of expired keys runs when the dict has doubled since the last
# one (never below this many keys), so its cost is amortised O(1) per admitted
# attempt however fast keys arrive -- and they can arrive fast: an unknown
# username never reaches PBKDF2, so a guesser rotating names and addresses is
# bounded only by the per-address budget.
_SWEEP_FLOOR = 64


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
        self._next_sweep = _SWEEP_FLOOR
        self._lock = threading.Lock()

    @staticmethod
    def _keys(username: str, ip: Optional[str]):
        # A digest, not the name: the request body puts no bound on the
        # username's length, and a key lives for a whole window.
        yield "user:" + hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()[:32]
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

    def reserve(self, username: str, ip: Optional[str]) -> Optional[int]:
        """Admit an attempt, counting it as a failure until `record_success`.

        Returns `None` when the attempt is admitted, else the whole seconds
        until the next one would be -- and then nothing is counted.
        """
        now = self._clock()
        with self._lock:
            worst = 0.0
            for key in self._keys(username, ip):
                window = self._prune(key, now)
                if window is not None and len(window) >= self._limit_for(key):
                    worst = max(worst, window[0] + self.window_seconds - now)
            if worst <= 0:
                for key in self._keys(username, ip):
                    self._failures.setdefault(key, deque()).append(now)
                if len(self._failures) >= self._next_sweep:
                    for key in list(self._failures):
                        self._prune(key, now)
                    self._next_sweep = max(_SWEEP_FLOOR, 2 * len(self._failures))
                return None
        return max(1, int(-(-worst // 1)))  # ceil

    def record_success(self, username: str, ip: Optional[str]) -> None:
        """The reserved attempt succeeded: forgive the username outright and
        give the address back the one slot this attempt took."""
        with self._lock:
            user_key, *ip_keys = self._keys(username, ip)
            self._failures.pop(user_key, None)
            for key in ip_keys:
                window = self._failures.get(key)
                if window:
                    window.pop()
                    if not window:
                        del self._failures[key]

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._failures)
