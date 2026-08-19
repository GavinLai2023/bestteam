"""Login rate limiting (beta gate G2).

The limiter is a pure in-memory sliding window over *failed* attempts, keyed
per username and per client IP; the API tests pin that `/api/auth/login`
consults it before it hashes a password, and that a successful login clears
the username's failures.
"""

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from helpers import create_user_and_login

from ui.backend import auth_api
from ui.backend.login_rate_limit import LoginRateLimiter


# --- pure limiter -----------------------------------------------------------


def _clock(start=1_000.0):
    state = {"now": start}

    def now():
        return state["now"]

    def advance(seconds):
        state["now"] += seconds

    return now, advance


def test_blocks_after_the_username_limit_within_the_window():
    now, advance = _clock()
    limiter = LoginRateLimiter(username_limit=3, ip_limit=100, window_seconds=60, clock=now)
    for _ in range(3):
        assert limiter.retry_after("alice", "1.1.1.1") is None
        limiter.record_failure("alice", "1.1.1.1")
    assert limiter.retry_after("alice", "1.1.1.1") == 60
    # The window is sliding: the oldest failure ages out first.
    advance(59)
    assert limiter.retry_after("alice", "1.1.1.1") == 1
    advance(1)
    assert limiter.retry_after("alice", "1.1.1.1") is None


def test_username_key_is_case_insensitive_and_ip_independent():
    now, _ = _clock()
    limiter = LoginRateLimiter(username_limit=2, ip_limit=100, window_seconds=60, clock=now)
    limiter.record_failure("Alice", "1.1.1.1")
    limiter.record_failure("alice", "2.2.2.2")
    assert limiter.retry_after("ALICE", "3.3.3.3") == 60
    assert limiter.retry_after("bob", "3.3.3.3") is None


def test_blocks_an_ip_across_usernames():
    now, _ = _clock()
    limiter = LoginRateLimiter(username_limit=100, ip_limit=3, window_seconds=60, clock=now)
    for name in ("a", "b", "c"):
        limiter.record_failure(name, "9.9.9.9")
    assert limiter.retry_after("d", "9.9.9.9") == 60
    assert limiter.retry_after("d", "8.8.8.8") is None


def test_success_clears_the_username_but_not_the_ip():
    now, _ = _clock()
    limiter = LoginRateLimiter(username_limit=2, ip_limit=3, window_seconds=60, clock=now)
    limiter.record_failure("alice", "1.1.1.1")
    limiter.record_failure("alice", "1.1.1.1")
    limiter.record_success("alice")
    assert limiter.retry_after("alice", "1.1.1.1") is None
    limiter.record_failure("bob", "1.1.1.1")
    # 3 failures from the IP in the window: the success did not forgive them.
    assert limiter.retry_after("carol", "1.1.1.1") == 60


def test_missing_ip_only_counts_against_the_username():
    now, _ = _clock()
    limiter = LoginRateLimiter(username_limit=100, ip_limit=1, window_seconds=60, clock=now)
    limiter.record_failure("alice", None)
    limiter.record_failure("bob", None)
    assert limiter.retry_after("carol", None) is None


def test_expired_keys_are_swept():
    now, advance = _clock()
    limiter = LoginRateLimiter(username_limit=5, ip_limit=5, window_seconds=60, clock=now)
    for i in range(50):
        limiter.record_failure(f"user{i}", f"10.0.0.{i}")
    assert limiter.tracked_keys() == 100
    advance(61)
    limiter.record_failure("late", "1.1.1.1")
    assert limiter.tracked_keys() == 2


# --- the login route --------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from helpers import make_concurrent_safe_engine
    from ui.backend import main as backend_main
    from ui.backend.db import init_db, session_factory
    from ui.backend.db_session import get_db

    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()
    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    TestSessionLocal = session_factory(engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[get_db] = override_get_db
    with TestClient(backend_main.app) as c:
        yield c
    backend_main.app.dependency_overrides.clear()


def _login(client, username, password):
    # Every TestClient request arrives from the same address ("testclient"),
    # so these tests exercise the username key; the IP key is covered by the
    # pure-limiter tests above.
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_login_returns_429_with_retry_after_once_the_username_is_throttled(client, monkeypatch):
    now, advance = _clock()
    monkeypatch.setattr(
        auth_api,
        "_LOGIN_LIMITER",
        LoginRateLimiter(username_limit=3, ip_limit=100, window_seconds=600, clock=now),
    )
    create_user_and_login(client, username="alice", password="hunter2")
    for _ in range(3):
        assert _login(client, "alice", "wrong").status_code == 401

    resp = _login(client, "alice", "hunter2")  # the RIGHT password is throttled too

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "600"
    assert "Too many failed login attempts" in resp.json()["detail"]
    advance(600)
    assert _login(client, "alice", "hunter2").status_code == 200


def test_throttle_check_happens_before_the_password_hash(client, monkeypatch):
    calls = []
    original = auth_api.authenticate_user

    def counting(db, username, password):
        calls.append(username)
        return original(db, username, password)

    monkeypatch.setattr(auth_api, "authenticate_user", counting)
    monkeypatch.setattr(
        auth_api, "_LOGIN_LIMITER", LoginRateLimiter(username_limit=1, ip_limit=100, window_seconds=600)
    )
    assert _login(client, "nobody", "x").status_code == 401
    assert _login(client, "nobody", "x").status_code == 429
    # PBKDF2 ran once; the throttled request never reached it -- that is the
    # CPU-exhaustion half of the defence.
    assert calls == ["nobody"]


def test_a_successful_login_clears_the_usernames_failures(client, monkeypatch):
    monkeypatch.setattr(
        auth_api, "_LOGIN_LIMITER", LoginRateLimiter(username_limit=2, ip_limit=100, window_seconds=600)
    )
    create_user_and_login(client, username="alice", password="hunter2")
    assert _login(client, "alice", "wrong").status_code == 401
    assert _login(client, "alice", "hunter2").status_code == 200
    assert _login(client, "alice", "wrong").status_code == 401
    assert _login(client, "alice", "wrong").status_code == 401
    assert _login(client, "alice", "wrong").status_code == 429


def test_the_shipped_defaults_are_conservative():
    limiter = LoginRateLimiter()
    assert limiter.username_limit == 5
    assert limiter.ip_limit == 20
    assert limiter.window_seconds == 15 * 60
