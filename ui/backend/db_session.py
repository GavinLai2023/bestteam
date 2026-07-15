"""Engine + per-request DB session for the FastAPI app (Phase 2).

`BESTTEAM_DB_PATH` overrides the default SQLite file location (relative to
this directory). Tests override the `get_db` dependency with an in-memory
database instead of touching this module-level engine -- see
`tests/test_builder_api.py`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from sqlalchemy.orm import Session

from .db import init_db, make_engine, session_factory
from .db.model_catalog import seed_default_catalog
from .db.users import reconcile_admins


def admin_usernames_from_env() -> list[str]:
    """Parse `BESTTEAM_ADMIN_USERS` (comma-separated usernames), ignoring blanks."""
    raw = os.environ.get("BESTTEAM_ADMIN_USERS", "")
    return [name.strip() for name in raw.split(",") if name.strip()]


DB_PATH = Path(os.environ.get("BESTTEAM_DB_PATH", str(Path(__file__).parent / "data" / "bestteam.db")))
if str(DB_PATH) != ":memory:":
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = make_engine(DB_PATH)
init_db(engine)
SessionLocal = session_factory(engine)

with SessionLocal() as _session:
    seed_default_catalog(_session)
    # Bootstrap admins from the env list (source of truth; idempotent).
    reconcile_admins(_session, admin_usernames_from_env())


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
