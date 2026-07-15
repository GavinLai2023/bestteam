"""Engine + per-request DB session for the FastAPI app (Phase 2).

`BESTTEAM_DB_PATH` overrides the default SQLite file location (relative to
this directory). Tests override the `get_db` dependency with an in-memory
database instead of touching this module-level engine -- see
`tests/test_builder_api.py`.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Iterator

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .db import init_db, make_engine, session_factory
from .db.model_catalog import seed_default_catalog
from .db.orgs import seed_default_org
from .skills import seed_default_skills

DB_PATH = Path(os.environ.get("BESTTEAM_DB_PATH", str(Path(__file__).parent / "data" / "bestteam.db")))
if str(DB_PATH) != ":memory:":
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = make_engine(DB_PATH)
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
