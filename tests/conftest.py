"""Shared pytest setup. Ensures ui.backend.main can be imported during tests
without tripping the BESTTEAM_SECRET_KEY startup guard in ui/backend/main.py
-- that guard refuses to start with the public dev-default secret -- and
without ui/backend/db_session.py's module-level engine touching a real,
possibly-contaminated local bestteam.db file: importing it creates and seeds
a SQLite database as a side effect of import, before any test gets a chance
to override the get_db dependency with its own in-memory database.

BESTTEAM_DB_PATH is force-set (not setdefault) rather than left to inherit
the ambient shell -- a developer with it pointed at a real per-deployment
database (a supported local-dev setup, see db_session.py) would otherwise
have that real file created/seeded/mutated by pytest collection, the exact
class of contamination this fixture exists to prevent. Nothing in the suite
needs the ambient value: test_migrations.py sets its own via monkeypatch
per-test, after this module has already run."""
import os

os.environ.setdefault("BESTTEAM_SECRET_KEY", "test-secret-key-not-for-production-use")
os.environ["BESTTEAM_DB_PATH"] = ":memory:"

import importlib.util

collect_ignore_glob = [] if importlib.util.find_spec("playwright") else ["e2e/*"]

# Password hashing is the single most expensive thing this suite does: at the
# production 260,000 PBKDF2 iterations a hash costs ~0.76s, and function-scoped
# fixtures that register and log in a few users pay it several times per test.
# Measured over `-m "not e2e"`, that is 543 of 789 seconds -- 69% of the suite.
#
# Lowering the module attribute (rather than adding an env var or a config key
# to auth.py) is deliberate: an env-tunable iteration count would be a
# permanent production misconfiguration risk, whereas this cannot be reached
# from outside a pytest process. hash_password reads the attribute at call
# time and verify_password reads the count back out of the stored hash string,
# so the two stay self-consistent at any value.
#
# This applies only in-process. tests/e2e/ drives a real uvicorn subprocess
# that never imports this file, so the e2e tier still exercises genuine 260k
# hashing -- and test_auth.py::test_production_pbkdf2_iterations_are_unchanged
# pins the shipped default so this can never mask a weakened one.
# auth.py is stdlib-only, so this import needs none of the optional extras --
# but an SDK-only checkout without ui/ should still be able to collect.
try:
    from ui.backend import auth as _auth
except ImportError:  # pragma: no cover - ui/ absent
    pass
else:
    _auth._PBKDF2_ITERATIONS = 1_000


# The login limiter is process-global state: without a reset, a handful of
# wrong-password logins spread across unrelated tests would 429 a later one.
# Imported lazily so collecting an SDK-only test never imports the backend.
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _fresh_login_limiter(monkeypatch):
    try:
        from ui.backend import auth_api
        from ui.backend.login_rate_limit import LoginRateLimiter
    except ImportError:  # pragma: no cover - ui/ absent or extras missing
        return
    monkeypatch.setattr(auth_api, "_LOGIN_LIMITER", LoginRateLimiter())


# Attribute `pytest_collection_modifyitems` below stashes the collected items'
# markers on, for `test_marker_completeness.py` to assert over. Shared as a
# literal rather than an import so neither file has to import the other.
_COLLECTED_MARKERS_ATTR = "bestteam_collected_markers"


@_pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Record every collected item's node id and marker names.

    `tryfirst` is load-bearing: pytest's own mark plugin does `-m` deselection
    in this same hook, so running first is what makes this the FULL collection
    rather than one CI job's slice. `test_marker_completeness.py` reads it back
    -- that used to cost a `--collect-only` subprocess (39 s, 6% of the suite)
    to learn what this process had already collected.
    """
    setattr(
        config,
        _COLLECTED_MARKERS_ATTR,
        [(item.nodeid, {mark.name for mark in item.iter_markers()}) for item in items],
    )
