"""Login rate limiting (beta gate G2).

The limiter is a pure in-memory sliding window over *failed* attempts, keyed
per username and per client IP, and the check reserves the attempt it admits;
the API tests pin that `/api/auth/login` consults it before it hashes a
password, and that a successful login clears the username's failures.
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
        assert limiter.reserve("alice", "1.1.1.1") is None  # admitted, and counted
    assert limiter.reserve("alice", "1.1.1.1") == 60
    # The window is sliding: the oldest failure ages out first.
    advance(59)
    assert limiter.reserve("alice", "1.1.1.1") == 1
    advance(1)
    assert limiter.reserve("alice", "1.1.1.1") is None


def test_the_check_itself_reserves_the_slot():
    # No separate "record failure" step exists to race against: once `limit`
    # attempts have been admitted -- none of them yet resolved -- the next is
    # refused. This is what makes a concurrent burst hash at most `limit`
    # times rather than once per pool thread.
    now, _ = _clock()
    limiter = LoginRateLimiter(username_limit=2, ip_limit=100, window_seconds=60, clock=now)
    assert limiter.reserve("alice", "1.1.1.1") is None
    assert limiter.reserve("alice", "1.1.1.1") is None
    assert limiter.reserve("alice", "1.1.1.1") == 60
    # A refused attempt is not counted, so the window is not extended by it.
    assert limiter.tracked_keys() == 2


def test_username_key_is_case_insensitive_and_ip_independent():
    now, _ = _clock()
    limiter = LoginRateLimiter(username_limit=2, ip_limit=100, window_seconds=60, clock=now)
    limiter.reserve("Alice", "1.1.1.1")
    limiter.reserve("alice", "2.2.2.2")
    assert limiter.reserve("ALICE", "3.3.3.3") == 60
    assert limiter.reserve("bob", "3.3.3.3") is None


def test_blocks_an_ip_across_usernames():
    now, _ = _clock()
    limiter = LoginRateLimiter(username_limit=100, ip_limit=3, window_seconds=60, clock=now)
    for name in ("a", "b", "c"):
        limiter.reserve(name, "9.9.9.9")
    assert limiter.reserve("d", "9.9.9.9") == 60
    assert limiter.reserve("d", "8.8.8.8") is None


def test_success_clears_the_username_and_releases_only_its_own_ip_slot():
    now, _ = _clock()
    limiter = LoginRateLimiter(username_limit=3, ip_limit=3, window_seconds=60, clock=now)
    limiter.reserve("alice", "1.1.1.1")  # fails
    limiter.reserve("alice", "1.1.1.1")  # fails
    assert limiter.reserve("alice", "1.1.1.1") is None  # succeeds:
    limiter.record_success("alice", "1.1.1.1")
    assert limiter.reserve("alice", "1.1.1.1") is None  # ...the username is forgiven,
    limiter.record_success("alice", "1.1.1.1")
    # ...but the address still carries the two failures: one more and it is out.
    limiter.reserve("bob", "1.1.1.1")
    assert limiter.reserve("carol", "1.1.1.1") == 60


def test_missing_ip_only_counts_against_the_username():
    now, _ = _clock()
    limiter = LoginRateLimiter(username_limit=100, ip_limit=1, window_seconds=60, clock=now)
    limiter.reserve("alice", None)
    limiter.reserve("bob", None)
    assert limiter.reserve("carol", None) is None
    limiter.record_success("carol", None)  # nothing to release; must not raise


def test_expired_keys_are_swept():
    now, advance = _clock()
    limiter = LoginRateLimiter(username_limit=5, ip_limit=5, window_seconds=60, clock=now)
    for i in range(50):
        limiter.reserve(f"user{i}", f"10.0.0.{i}")
    assert limiter.tracked_keys() == 100
    advance(61)
    limiter.reserve("late", "1.1.1.1")
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
