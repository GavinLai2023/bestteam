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
