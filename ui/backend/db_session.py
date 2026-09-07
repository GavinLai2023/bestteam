"""Engine + per-request DB session for the FastAPI app (Phase 2).

The database is chosen by `db.database.resolve_database_url`:
`BESTTEAM_DATABASE_URL` (sqlite or postgresql) wins, else the legacy
`BESTTEAM_DB_PATH` (a SQLite file, default `ui/backend/data/bestteam.db`).
Tests override the `get_db` dependency with their own engine instead of
touching this module-level one -- see `tests/helpers.py`.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Iterator

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .db import init_db, make_engine, session_factory
from .db.database import DATA_DIR, lock_anchor_for, resolve_database_url, sqlite_path_of
from .db.model_catalog import seed_default_catalog
from .db.orgs import ensure_email_single_org, seed_default_org
from .skills import seed_default_skills

DATABASE_URL = resolve_database_url(os.environ)
# The SQLite file behind DATABASE_URL, or None for `:memory:` and for a
# server database. Kept for callers that only make sense for a file.
DB_PATH = sqlite_path_of(DATABASE_URL)
if DB_PATH is not None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
# What the single-instance lock is keyed on (`process_lock.py`): the SQLite
# file, `:memory:` (no lock), or `data/bestteam.lock` for a server database.
LOCK_ANCHOR = lock_anchor_for(DATABASE_URL, DATA_DIR)
if isinstance(LOCK_ANCHOR, Path):
    LOCK_ANCHOR.parent.mkdir(parents=True, exist_ok=True)

engine = make_engine(DATABASE_URL)
init_db(engine)
SessionLocal = session_factory(engine)

# Admins are provisioned deliberately via the `ui.backend.admin` CLI, not
# bootstrapped from env at import -- so startup never reads `users.is_admin`.
# Seeding queries columns added by migrations (e.g. `skills.org_id`), so on a
# database predating the latest migration it is skipped with a warning rather
# than crashing the import -- the process must still boot far enough to run
# `alembic upgrade head` / the operator CLI (same property as CR-025).
with SessionLocal() as _session:
    try:
        seed_default_org(_session)
        seed_default_catalog(_session)
        seed_default_skills(_session)
        # Multi-org + process-wide email creds would expose one customer's
        # mailbox to every tenant -- refuse to boot (CR-031). The RuntimeError
        # deliberately escapes the OperationalError catch below.
        ensure_email_single_org(_session)
        # NB: the secrets-key / stored-credential guard runs in `main.py` at
        # app import, NOT here -- the operator CLI (`ui.backend.admin`) imports
        # this module for recovery (`clear-email`/`set-email`) and must boot
        # even when the key can't decrypt existing rows.
    except OperationalError as _exc:
        warnings.warn(
            "Skipping default-data seeding: the database schema predates the "
            f"latest migration ({_exc.orig}). Run `alembic upgrade head`, then "
            "restart the backend.",
            RuntimeWarning,
            stacklevel=1,
        )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
