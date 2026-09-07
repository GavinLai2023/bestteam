# Database Engine Portability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backend, Alembic, the operator CLI and the test suite work against either SQLite or Postgres from one connection URL, prove the Postgres path in CI, and ship a tested `admin migrate-db` — while production, local development and the test default all stay on SQLite.

**Architecture:** One resolver (`BESTTEAM_DATABASE_URL`, else the legacy `BESTTEAM_DB_PATH`) feeds one engine factory that accepts a path, `:memory:` or a URL; the handful of SQLite-specific spots become dialect-neutral; foreign keys are enforced in the *test* engine so the SQLite suite surfaces what Postgres would refuse; a `backend-postgres` CI job clones a fresh Postgres database per test from a template; `migrate-db` copies a database into an empty one through the ORM's table metadata with pre-flight checks, an explicit orphan policy, sequence reset and verification.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, Alembic, FastAPI, pytest; `psycopg[binary]` 3 as the Postgres driver; GitHub Actions `services: postgres:16`; a portable PostgreSQL 16 binaries zip for local runs (no Docker on the workstation).

**Spec:** `docs/superpowers/specs/2026-09-07-database-engine-portability-design.md` — every task cites its section. Read the spec's §2 (findings) and §15 (risks) before starting.

## Global Constraints

- Python floor is `>=3.10` (`pyproject.toml`); CI runs 3.11; the workstation venv is 3.13 at `./.venv/Scripts/python.exe`. Run everything through that venv.
- Supported engines are exactly **sqlite** and **postgresql**. Any other dialect is refused by name.
- Production keeps running one uvicorn process on one SQLite file. Nothing in this plan changes the live server; the ops half is a later spec.
- Local development and the test suite's default stay on SQLite. A new developer needs no database server.
- The production SQLite file keeps foreign-key enforcement **off**; only the test engine turns it on.
- Every new test file carries a `pytestmark` (`unit` / `integration` / `e2e` / `optional`, optionally `slow`) — `tests/test_marker_completeness.py` fails the suite otherwise.
- `-n auto` is fine for local SQLite runs; the Postgres lane runs **serially**. Never run `-n auto` on `tests/e2e/`.
- After changing a dependency in `pyproject.toml`, regenerate the lockfile with the exact command in the root `CLAUDE.md` (`uv pip compile … -o requirements.lock`).
- Write code comments in English. British spelling in prose and docs (organisation, serialise).
- Commit after every task with the trailer:

  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01NrEyA4KWApNs9CEn115EbN
  ```

- The eight rulings in the spec are settled; do not re-open them. In particular: plain `JSON`, never JSONB (Ruling 3); a fresh Postgres schema comes from `alembic upgrade head` (Ruling 6); orphan policy = nullable FK → NULL, NOT NULL FK → skip the row, refuse by default (Ruling 8).

## Delivery shape

Three PRs, in order, each green on its own (spec §14). Branch for all three: `feat/db-engine-portability` (already cut from `main` at `244da8b`; the spec is committed on it). Open PR 1 from it; PRs 2 and 3 are stacked branches cut from the previous PR's head (`feat/db-engine-portability-tests`, `feat/db-engine-portability-migrate`) and re-targeted at `main` once the previous one merges.

| PR | Tasks | Done when |
|---|---|---|
| 1 | 1–10 | SQLite suite green; `BESTTEAM_DATABASE_URL=sqlite:///…` runs the backend, Alembic and `check-env` end to end; `check-env` prints the `database:` line |
| 2 | 11–17 | SQLite suite green with foreign keys enforced; `backend-postgres` green on the PR; the Postgres migration-replay test passes |
| 3 | 18–22 | both `migrate-db` tests green; a local rehearsal into the portable Postgres recorded in the PR |

## File structure

**PR 1**

- Modify `ui/backend/db/database.py` — URL resolver, `sqlite_url_for`, `sqlite_path_of`, `describe_database_url`, `lock_anchor_for`, `readonly_engine`; `make_engine` accepts URLs.
- Modify `ui/backend/db/__init__.py` — export the new helpers.
- Modify `ui/backend/db_session.py` — resolver; exports `DATABASE_URL`, `DB_PATH` (now `Optional[Path]`), `LOCK_ANCHOR`.
- Modify `ui/backend/main.py:84,168` — lock keyed on `LOCK_ANCHOR`.
- Modify `ui/backend/process_lock.py` — docstring only.
- Modify `alembic/env.py` — resolver; a preset `sqlalchemy.url` wins; `%` escaped.
- Modify `ui/backend/db/models.py:16,66,80-85,328` — `true()` defaults, `postgresql_where`.
- Modify `alembic/versions/{a3f7c9d2e6b1,c1d2e3f4a5b6,f2a3b4c5d6e7,v9w0x1y2z3a4}_*.py` — boolean defaults.
- Modify `ui/backend/db/inbox_events.py` — dialect switch for the dedup insert.
- Modify `ui/backend/env_check.py` — `database` finding; the three checks read through an engine.
- Modify `ui/backend/admin.py` — `check-env` / `check-health` use the URL.
- Modify `pyproject.toml`, regenerate `requirements.lock` — `psycopg[binary]` in the `ui` extra.
- Modify docs: `docs/DECISIONS.md`, `docs/deployment.md`, `.env.example`, `ui/backend/db/CLAUDE.md`, `ui/backend/CLAUDE.md`, `docs/ADMIN_GUIDE.md`, `docs/STATUS.md`.
- Create tests: `tests/test_database_url.py`, `tests/test_schema_dialects.py`, `tests/test_inbox_events_dialect.py`; extend `tests/test_env_check.py`, `tests/test_migrations.py`.

**PR 2**

- Create `tests/_postgres.py` — template database, per-test clones, drops.
- Modify `tests/helpers.py` — `make_test_engine(tmp_path=None)` replaces `make_concurrent_safe_engine`.
- Modify `tests/conftest.py` — per-test drop fixture, session-end cleanup, `sqlite_only` skip on the lane.
- Modify `pyproject.toml` — `sqlite_only` marker.
- Modify every `tests/test_*.py` that calls `make_engine(":memory:")` or `make_concurrent_safe_engine(...)`.
- Modify `ui/backend/runtime.py:1148-1180` — the failure path writes the run row before its terminal trace.
- Create `tests/test_test_engine.py`, `tests/test_runtime_failure_path.py`, `tests/test_migrations_postgres.py`.
- Modify `.github/workflows/ci.yml` — `backend-postgres` job.
- Modify docs: `ui/backend/db/CLAUDE.md`, root `CLAUDE.md` (one testing bullet), `docs/STATUS.md`.

**PR 3**

- Create `ui/backend/db/migrate.py` — orphan report, pre-flight, copy, sequence reset, verification, `run_migration`.
- Modify `ui/backend/admin.py` — `migrate-db` subcommand.
- Create `tests/test_migrate_db.py`.
- Modify docs: `docs/ADMIN_GUIDE.md`, `docs/deployment.md`, `docs/STATUS.md`.

---

## PR 1 — one URL, dialect-neutral code, the driver in the image

### Task 1: URL resolution helpers (spec §3)

**Files:**
- Modify: `ui/backend/db/database.py`
- Modify: `ui/backend/db/__init__.py`
- Test: `tests/test_database_url.py`

**Interfaces:**
- Produces (all in `ui/backend/db/database.py`, re-exported from `ui.backend.db`):
  - `DATA_DIR: Path` (= `ui/backend/data`), `DEFAULT_DB_PATH: Path`, `MEMORY_URL = "sqlite:///:memory:"`
  - `sqlite_url_for(path: str | Path) -> str`
  - `resolve_database_url(env: Mapping[str, str] | None = None) -> str`
  - `sqlite_path_of(url: str) -> Path | None`
  - `describe_database_url(url: str) -> str` (never contains a password)
  - `lock_anchor_for(url: str, data_dir: str | Path = DATA_DIR) -> str | Path`
  - `readonly_engine(url: str) -> Engine`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_database_url.py`:

```python
"""URL resolution and engine construction for the two supported engines
(spec 2026-09-07-database-engine-portability, section 3)."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
pytest.importorskip("sqlalchemy")

from sqlalchemy.exc import OperationalError

from ui.backend.db.database import (
    DEFAULT_DB_PATH,
    describe_database_url,
    lock_anchor_for,
    readonly_engine,
    resolve_database_url,
    sqlite_path_of,
    sqlite_url_for,
)


def test_url_wins_over_path():
    env = {"BESTTEAM_DATABASE_URL": "postgresql+psycopg://u:p@db/x", "BESTTEAM_DB_PATH": "/tmp/a.db"}
    assert resolve_database_url(env) == "postgresql+psycopg://u:p@db/x"


def test_path_becomes_a_sqlite_url_and_the_default_is_the_data_dir():
    assert resolve_database_url({"BESTTEAM_DB_PATH": "/tmp/a.db"}) == f"sqlite:///{Path('/tmp/a.db')}"
    assert resolve_database_url({"BESTTEAM_DB_PATH": ":memory:"}) == "sqlite:///:memory:"
    assert resolve_database_url({}) == f"sqlite:///{DEFAULT_DB_PATH}"


def test_a_blank_url_falls_back_to_the_path():
    env = {"BESTTEAM_DATABASE_URL": "   ", "BESTTEAM_DB_PATH": ":memory:"}
    assert resolve_database_url(env) == "sqlite:///:memory:"


def test_sqlite_path_of():
    assert sqlite_path_of(sqlite_url_for(Path("/tmp/a.db"))) == Path("/tmp/a.db")
    assert sqlite_path_of("sqlite:///:memory:") is None
    assert sqlite_path_of("postgresql+psycopg://u:p@db/x") is None


def test_describe_never_shows_the_password():
    line = describe_database_url("postgresql+psycopg://alice:s3cret@db.example:5432/bestteam")
    assert "s3cret" not in line
    assert line == "postgresql host db.example database bestteam"
    assert describe_database_url("sqlite:///:memory:") == "sqlite in-memory"
    assert describe_database_url(sqlite_url_for("/tmp/a.db")).startswith("sqlite file ")


def test_lock_anchor_follows_the_engine():
    assert lock_anchor_for(sqlite_url_for("/tmp/a.db")) == Path("/tmp/a.db")
    assert lock_anchor_for("sqlite:///:memory:") == ":memory:"
    assert lock_anchor_for("postgresql+psycopg://u:p@db/x", data_dir="/data") == Path("/data") / "bestteam"


def test_readonly_engine_does_not_create_a_missing_sqlite_file(tmp_path):
    missing = tmp_path / "absent.db"
    engine = readonly_engine(sqlite_url_for(missing))
    with pytest.raises(OperationalError):
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    assert not missing.exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_database_url.py -v`
Expected: FAIL at import — `cannot import name 'describe_database_url'`.

- [ ] **Step 3: Implement the helpers**

In `ui/backend/db/database.py`, replace the import block (lines 13–20) with:

```python
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Mapping, Optional, Union
from urllib.request import pathname2url

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from .models import Base

# ui/backend/data -- the volume that holds the SQLite file, uploads and the
# per-user memory store. `db_session.py` and `env_check.py` take it from here.
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_DB_PATH = DATA_DIR / "bestteam.db"
MEMORY_URL = "sqlite:///:memory:"


def sqlite_url_for(path: Union[str, Path]) -> str:
    """The SQLAlchemy URL for a SQLite file path (or the `:memory:` literal)."""
    if str(path) == ":memory:":
        return MEMORY_URL
    return f"sqlite:///{Path(path)}"


def resolve_database_url(env: Optional[Mapping[str, str]] = None) -> str:
    """Which database this process uses, as a SQLAlchemy URL.

    `BESTTEAM_DATABASE_URL` wins when set. Otherwise the legacy
    `BESTTEAM_DB_PATH` (a SQLite file path, or `:memory:`) becomes a
    `sqlite:///` URL, defaulting to `ui/backend/data/bestteam.db`. This is the
    one place either variable is read: `db_session.py`, `alembic/env.py`,
    `env_check.py` and `admin.py` all call it.
    """
    env = os.environ if env is None else env
    url = (env.get("BESTTEAM_DATABASE_URL") or "").strip()
    if url:
        return url
    path = (env.get("BESTTEAM_DB_PATH") or "").strip() or str(DEFAULT_DB_PATH)
    return sqlite_url_for(path)


def sqlite_path_of(url: str) -> Optional[Path]:
    """The file behind a SQLite file URL; None for `:memory:` or another engine."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        return None
    return Path(parsed.database)


def describe_database_url(url: str) -> str:
    """One line naming the database for humans. Never includes a password."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        path = sqlite_path_of(url)
        return f"sqlite file {path}" if path is not None else "sqlite in-memory"
    return f"{parsed.get_backend_name()} host {parsed.host or '?'} database {parsed.database or '?'}"


def lock_anchor_for(url: str, data_dir: Union[str, Path] = DATA_DIR) -> Union[str, Path]:
    """What `process_lock.acquire_single_instance_lock` is keyed on.

    A SQLite file keeps its lock beside the file (`<file>.lock`, unchanged
    across upgrades); `:memory:` returns the literal, which the lock skips;
    a server database locks `<data_dir>/bestteam.lock` -- the lock guards
    this host's single process, so it lives on this host's data volume.
    """
    path = sqlite_path_of(url)
    if path is not None:
        return path
    if url == MEMORY_URL:
        return ":memory:"
    return Path(data_dir) / "bestteam"


def readonly_engine(url: str) -> Engine:
    """An engine that can only read.

    A SQLite file is opened through a `file:...?mode=ro` URI, so a checklist
    or a copy can neither create the file nor write to one that exists (the
    trick `env_check` used with stdlib sqlite3; read-only still reads a live
    WAL database). A server database is opened normally; callers only SELECT.
    """
    path = sqlite_path_of(url)
    if path is not None:
        uri = "file:" + pathname2url(str(path)) + "?mode=ro"
        return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True), poolclass=NullPool)
    return create_engine(url, poolclass=NullPool)
```

Keep the existing `make_engine`, `init_db` and `session_factory` below unchanged for now (Task 2 rewrites `make_engine`). Then in `ui/backend/db/__init__.py` change the first line to:

```python
from .database import (
    DATA_DIR,
    DEFAULT_DB_PATH,
    MEMORY_URL,
    describe_database_url,
    init_db,
    lock_anchor_for,
    make_engine,
    readonly_engine,
    resolve_database_url,
    session_factory,
    sqlite_path_of,
    sqlite_url_for,
)
```

and add the same names to `__all__`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_database_url.py tests/test_db.py -v`
Expected: all PASS (the `readonly` test relies on `pathname2url` producing `///C:/...` on Windows, which `env_check` already depends on).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/database.py ui/backend/db/__init__.py tests/test_database_url.py
git commit -m "feat(db): resolve the database from one URL, with read-only and lock-anchor helpers"
```

### Task 2: `make_engine` accepts a URL (spec §3)

**Files:**
- Modify: `ui/backend/db/database.py` (`make_engine`)
- Test: `tests/test_database_url.py`

**Interfaces:**
- Produces: `make_engine(target: str | Path = "bestteam.db", *, echo: bool = False) -> Engine` accepting `":memory:"`, `"sqlite:///:memory:"`, a file path, or any URL. Raises `ValueError` (unparseable URL) or `RuntimeError` (driver missing), both naming `BESTTEAM_DATABASE_URL`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_database_url.py`:

```python
from sqlalchemy.pool import StaticPool

from ui.backend.db.database import make_engine


def test_make_engine_memory_url_uses_the_static_pool():
    assert isinstance(make_engine("sqlite:///:memory:").pool, StaticPool)
    assert isinstance(make_engine(":memory:").pool, StaticPool)


def test_make_engine_path_and_sqlite_url_are_the_same_engine(tmp_path):
    by_path = make_engine(tmp_path / "a.db")
    by_url = make_engine(sqlite_url_for(tmp_path / "a.db"))
    try:
        assert by_path.url == by_url.url
        with by_url.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
    finally:
        by_path.dispose()
        by_url.dispose()


def test_make_engine_names_the_variable_on_a_bad_url():
    with pytest.raises(ValueError, match="BESTTEAM_DATABASE_URL"):
        make_engine("://")
    with pytest.raises(RuntimeError, match="BESTTEAM_DATABASE_URL"):
        make_engine("nosuchdb://user:pw@host/db")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_database_url.py -v -k make_engine`
Expected: the memory-URL and bad-URL tests FAIL (today a URL is treated as a file path named `sqlite:///:memory:`).

- [ ] **Step 3: Rewrite `make_engine`**

Replace the whole `make_engine` function in `ui/backend/db/database.py` with:

```python
def make_engine(target: Union[str, Path] = "bestteam.db", *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine for a path, `:memory:`, or a database URL.

    Three shapes:

    - `":memory:"` (or `sqlite:///:memory:`): an ephemeral database on a
      `StaticPool`, so every connection shares the one in-memory database --
      the default pooling would hand out a fresh, empty database per
      connection (tests, dry runs).
    - a file path (anything without `://`): the per-deployment SQLite file.
    - a URL: a SQLite URL behaves like the path form; any other engine gets
      `pool_pre_ping` so a dropped server connection is replaced rather than
      surfaced as the next query's error. Only sqlite and postgresql are
      supported (`BESTTEAM_DATABASE_URL`).
    """
    target_str = str(target)
    if target_str in (":memory:", MEMORY_URL):
        return create_engine(
            MEMORY_URL,
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    url = target_str if "://" in target_str else sqlite_url_for(target_str)
    try:
        backend = make_url(url).get_backend_name()
    except ArgumentError as exc:
        raise ValueError(f"BESTTEAM_DATABASE_URL is not a valid database URL: {exc}") from exc

    if backend == "sqlite":
        engine = create_engine(url, echo=echo)

        @event.listens_for(engine, "connect")
        def _use_wal(dbapi_connection, _record):
            # One file is shared by the run workers, the ingestion executor,
            # the email poller and every request. In the default
            # rollback-journal mode a write transaction blocks every reader
            # until it commits; WAL lets readers proceed beside one writer.
            # The mode is persisted in the file, so repeating it per
            # connection is cheap and makes a fresh file correct from its
            # very first connection. (Busy waiting needs no setting:
            # pysqlite's default timeout is already 5 s.)
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    try:
        return create_engine(url, echo=echo, pool_pre_ping=True)
    except (ModuleNotFoundError, NoSuchModuleError) as exc:
        raise RuntimeError(
            f"BESTTEAM_DATABASE_URL names the {backend!r} engine but its driver is not "
            f"installed ({exc}); install the `ui` extra: pip install 'bestteam[ui]'"
        ) from exc
```

Also update the module docstring's first line to `"""Engine/session setup for the per-deployment database (SQLite by default, Postgres by URL)."""`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_database_url.py tests/test_db.py tests/test_migrations.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/database.py tests/test_database_url.py
git commit -m "feat(db): make_engine accepts a database URL, not only a file path"
```

### Task 3: wire the resolver into `db_session`, the instance lock and `main` (spec §3)

**Files:**
- Modify: `ui/backend/db_session.py:1-30`
- Modify: `ui/backend/main.py:84,168`
- Modify: `ui/backend/process_lock.py:1-17` (docstring)

**Interfaces:**
- Produces in `ui.backend.db_session`: `DATABASE_URL: str`, `DB_PATH: Optional[Path]` (the SQLite file, or `None`), `LOCK_ANCHOR: str | Path`. `engine`, `SessionLocal`, `get_db` unchanged.
- Consumes: Task 1's `resolve_database_url`, `sqlite_path_of`, `lock_anchor_for`, `DATA_DIR`.

- [ ] **Step 1: Rewrite the top of `db_session.py`**

Replace lines 1–29 (docstring through `engine = make_engine(DB_PATH)`) with:

```python
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
```

Keep `init_db(engine)` and everything after it exactly as it is.

- [ ] **Step 2: Point `main.py` at the anchor**

Line 84: `from .db_session import DB_PATH, SessionLocal, get_db` → `from .db_session import LOCK_ANCHOR, SessionLocal, get_db`.
Line 168: `process_lock.acquire_single_instance_lock(DB_PATH)` → `process_lock.acquire_single_instance_lock(LOCK_ANCHOR)`.

Check nothing else in `main.py` uses `DB_PATH`: `grep -n "DB_PATH" ui/backend/main.py` must print nothing.

- [ ] **Step 3: Update the lock's docstring**

In `ui/backend/process_lock.py`, replace the sentence
`it takes a non-blocking exclusive OS lock on \`<db>.lock\` next to the database file`
with
`it takes a non-blocking exclusive OS lock on \`<anchor>.lock\` -- beside the SQLite file, or \`data/bestteam.lock\` when the engine is a server database (\`db_session.LOCK_ANCHOR\`)`.
Rename the parameter `db_path` to `anchor` in `acquire_single_instance_lock` and its body (`lock_path = Path(f"{anchor}.lock")`); the `":memory:"` check stays.

- [ ] **Step 4: Verify with the existing suite and a manual boot**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_process_lock.py tests/test_startup_run_sweep.py tests/test_builder_api.py -q` (if `test_process_lock.py` does not exist, skip it)
Expected: PASS.

Then boot the backend against an explicit URL and hit health (in PowerShell):

```powershell
$env:BESTTEAM_SECRET_KEY = "dev-only-secret-change-me-for-real-use"
$env:BESTTEAM_DATABASE_URL = "sqlite:///$PWD\ui\backend\data\url-smoke.db"
.\.venv\Scripts\python.exe -m uvicorn ui.backend.main:app --port 8000 --host 127.0.0.1
```

In a second shell: `curl -s http://127.0.0.1:8000/api/health` → `{"status":"ok"...}`. Confirm `ui/backend/data/url-smoke.db` and `url-smoke.db.lock` exist. Stop the server, delete both files, unset the variable.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db_session.py ui/backend/main.py ui/backend/process_lock.py
git commit -m "feat(db): db_session, the instance lock and main follow BESTTEAM_DATABASE_URL"
```

### Task 4: Alembic follows the same URL, and a preset URL wins (spec §3)

**Files:**
- Modify: `alembic/env.py:14-23`
- Test: `tests/test_migrations.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_migrations.py`:

```python
def test_a_preset_config_url_beats_the_environment(tmp_path, monkeypatch):
    """`alembic/env.py` must not override a URL the caller set on the Config --
    that is how `admin migrate-db` and the Postgres replay test target a
    database other than the deployment's."""
    elsewhere = tmp_path / "env.db"
    monkeypatch.setenv("BESTTEAM_DB_PATH", str(elsewhere))
    target = tmp_path / "preset.db"
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{target}".replace("%", "%%"))

    command.upgrade(cfg, "head")

    assert target.exists()
    assert not elsewhere.exists()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py -v -k preset`
Expected: FAIL — `elsewhere` exists (env.py overrode the preset URL).

- [ ] **Step 3: Change `alembic/env.py`**

Replace lines 14–23 (`from ui.backend.db.models import Base` through `config.set_main_option(...)`) with:

```python
from ui.backend.db.database import resolve_database_url
from ui.backend.db.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# The URL comes from the same resolver the backend uses (BESTTEAM_DATABASE_URL,
# else BESTTEAM_DB_PATH) -- unless the caller already set `sqlalchemy.url` on
# the Config (the migration tests, `admin migrate-db`), which wins. `%` is
# doubled because ConfigParser interpolates it.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", resolve_database_url(os.environ).replace("%", "%%"))
```

Remove the now-unused `from pathlib import Path` only if nothing else in the file uses `Path` (it is still used by the `sys.path.insert` line — keep it).

- [ ] **Step 4: Run the migration tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py -v`
Expected: all PASS, including the existing `BESTTEAM_DB_PATH`-driven ones.

- [ ] **Step 5: Commit**

```bash
git add alembic/env.py tests/test_migrations.py
git commit -m "feat(alembic): migrate whatever BESTTEAM_DATABASE_URL resolves to; a preset Config URL wins"
```

### Task 5: dialect-neutral schema — partial index and boolean defaults (spec §4)

**Files:**
- Modify: `ui/backend/db/models.py:16,66,80-85,328`
- Modify: `alembic/versions/a3f7c9d2e6b1_add_share_links.py:41`, `c1d2e3f4a5b6_automation_item_results.py:49`, `f2a3b4c5d6e7_add_email_triggers.py:40`, `v9w0x1y2z3a4_add_pipelines_active.py:51`
- Test: `tests/test_schema_dialects.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_schema_dialects.py`:

```python
"""The schema compiles to the intended DDL on both supported dialects (spec §4)."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from ui.backend.db.models import Organization, PipelineRecord, User

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _index(table, name):
    return next(index for index in table.indexes if index.name == name)


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()], ids=["sqlite", "postgresql"])
def test_the_one_member_per_org_index_is_partial_on_both_dialects(dialect):
    ddl = str(CreateIndex(_index(User.__table__, "uq_users_org_id_not_null")).compile(dialect=dialect))
    assert "WHERE org_id IS NOT NULL" in ddl


@pytest.mark.parametrize("model", [Organization, PipelineRecord])
def test_active_defaults_to_a_boolean_on_postgres_and_1_on_sqlite(model):
    on_postgres = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
    on_sqlite = str(CreateTable(model.__table__).compile(dialect=sqlite.dialect()))
    assert "DEFAULT true" in on_postgres
    assert "DEFAULT 1" not in on_postgres
    assert "DEFAULT 1" in on_sqlite


def test_no_migration_uses_an_integer_literal_as_a_boolean_default():
    # Postgres rejects `DEFAULT 1` on a boolean column, so a fresh Postgres
    # schema could never be built from the chain (Ruling 6 depends on it).
    pattern = re.compile(r'sa\.Boolean\(\)[^\n]*server_default=sa\.text\("[01]"\)')
    offenders = sorted(p.name for p in _VERSIONS.glob("*.py") if pattern.search(p.read_text(encoding="utf-8")))
    assert offenders == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_schema_dialects.py -v`
Expected: the postgresql index case FAILS (no WHERE), both `DEFAULT` tests FAIL, the migration scan FAILS naming the four files.

- [ ] **Step 3: Fix the models**

`ui/backend/db/models.py` line 16:
`from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, UniqueConstraint, text, true`

Line 66 and line 328: `server_default=text("1")` → `server_default=true()`.

Lines 80–85, the `Index(...)` in `User.__table_args__`:

```python
        Index(
            "uq_users_org_id_not_null",
            "org_id",
            unique=True,
            sqlite_where=text("org_id IS NOT NULL"),
            postgresql_where=text("org_id IS NOT NULL"),
        ),
```

- [ ] **Step 4: Fix the four migrations**

Run from the repo root (Git Bash):

```bash
sed -i 's/server_default=sa\.text("1")/server_default=sa.true()/; s/server_default=sa\.text("0")/server_default=sa.false()/' \
  alembic/versions/a3f7c9d2e6b1_add_share_links.py \
  alembic/versions/c1d2e3f4a5b6_automation_item_results.py \
  alembic/versions/f2a3b4c5d6e7_add_email_triggers.py \
  alembic/versions/v9w0x1y2z3a4_add_pipelines_active.py
git diff --stat alembic/versions
```

Expected: exactly four files, one line each. Add one comment line above the changed line in `v9w0x1y2z3a4_add_pipelines_active.py` (the only one of the four with room for it): `# sa.true(): renders as 1 on SQLite and true on Postgres; sa.text("1") is rejected by Postgres.`

- [ ] **Step 5: Run the tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_schema_dialects.py tests/test_db.py tests/test_migrations.py -v`
Expected: all PASS (the migration chain still replays on SQLite with identical DDL).

- [ ] **Step 6: Commit**

```bash
git add ui/backend/db/models.py alembic/versions tests/test_schema_dialects.py
git commit -m "fix(db): boolean defaults and the users partial index compile on Postgres too"
```

### Task 6: the dedup insert follows the session's dialect (spec §4)

**Files:**
- Modify: `ui/backend/db/inbox_events.py:23,76-116`
- Test: `tests/test_inbox_events_dialect.py`

**Interfaces:**
- Produces: `_insert_for(db: Session)` returning `sqlalchemy.dialects.sqlite.insert` or `sqlalchemy.dialects.postgresql.insert`; raises `NotImplementedError` naming any other dialect.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inbox_events_dialect.py`:

```python
"""`record_events` picks the ON CONFLICT insert for the session's dialect (spec §4)."""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit
pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import postgresql, sqlite

from ui.backend.db.inbox_events import _insert_for


def _session_on(dialect_name):
    return SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name=dialect_name)))


def test_the_insert_construct_follows_the_session_dialect():
    assert _insert_for(_session_on("sqlite")) is sqlite.insert
    assert _insert_for(_session_on("postgresql")) is postgresql.insert


def test_other_dialects_are_refused_by_name():
    with pytest.raises(NotImplementedError, match="mysql"):
        _insert_for(_session_on("mysql"))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_inbox_events_dialect.py -v`
Expected: FAIL — `cannot import name '_insert_for'`.

- [ ] **Step 3: Implement**

In `ui/backend/db/inbox_events.py` replace line 23 (`from sqlalchemy.dialects.sqlite import insert as sqlite_insert`) with:

```python
from sqlalchemy.dialects import postgresql, sqlite
```

Add above `record_events`:

```python
def _insert_for(db: Session):
    """The dialect-specific INSERT that supports `on_conflict_do_nothing`.

    SQLAlchemy's generic `insert()` has no ON CONFLICT; the sqlite and
    postgresql constructs share the same call, so the choice is the only
    dialect-specific line in this module (spec 2026-09-07 §4).
    """
    name = db.get_bind().dialect.name
    if name == "sqlite":
        return sqlite.insert
    if name == "postgresql":
        return postgresql.insert
    raise NotImplementedError(
        f"record_events: no ON CONFLICT insert for dialect {name!r}; bestteam supports sqlite and postgresql"
    )
```

In `record_events`, replace the docstring sentence
`` `on_conflict_do_nothing` is SQLite-specific -- one of the places a future Postgres migration would touch (that dialect offers the same call). ``
with
`` `on_conflict_do_nothing` needs the dialect's own insert construct -- `_insert_for` picks it. ``
and the statement:

```python
    result = db.execute(
        _insert_for(db)(InboxEvent)
        .values(rows)
        .on_conflict_do_nothing(index_elements=_IDENTITY_COLUMNS)
    )
```

- [ ] **Step 4: Run the tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_inbox_events_dialect.py tests/test_inbox_events.py tests/test_email_trigger.py -q`
Expected: PASS (the SQLite path is unchanged end to end).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/db/inbox_events.py tests/test_inbox_events_dialect.py
git commit -m "feat(db): record_events picks the ON CONFLICT insert for the session's dialect"
```

### Task 7: `check-env` and `check-health` read through the engine (spec §6)

**Files:**
- Modify: `ui/backend/env_check.py:24-30,46-60,195-420`
- Modify: `ui/backend/admin.py:68-74,201-231`
- Test: `tests/test_env_check.py`, `tests/test_trigger_metrics.py` (existing tests must keep passing)

**Interfaces:**
- Produces in `env_check.py`: `check_database_url(env) -> Finding` (name `"database"`, appended by `check_environment`), `default_database_url(env) -> str`, and `check_schema(target, *, script_location=None)`, `check_org_retention(target)`, `check_model_catalog(target)` where `target` is a URL **or** the historical file path (`None` = the default file).
- Removes: `default_db_path`, `_DEFAULT_DB_PATH`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_env_check.py`:

```python
def test_database_line_names_the_sqlite_file_by_default():
    finding = _by_name(check_environment(_GOOD))["database"]
    assert finding.level == "OK"
    assert finding.message.startswith("sqlite file ")


def test_database_line_hides_the_password_and_warns_when_both_variables_are_set():
    pytest.importorskip("psycopg")
    env = dict(_GOOD, BESTTEAM_DATABASE_URL="postgresql+psycopg://bt:s3cretpw@db.internal:5432/bestteam")
    finding = _by_name(check_environment(env))["database"]
    assert finding.level == "OK"
    assert "s3cretpw" not in finding.message
    assert "db.internal" in finding.message

    both = dict(env, BESTTEAM_DB_PATH="/srv/bestteam.db")
    finding = _by_name(check_environment(both))["database"]
    assert finding.level == "WARN"
    assert "the URL wins" in finding.message


def test_database_line_fails_on_garbage_and_on_unsupported_engines():
    garbage = _by_name(check_environment(dict(_GOOD, BESTTEAM_DATABASE_URL="://")))["database"]
    assert garbage.level == "FAIL"
    mysql = _by_name(check_environment(dict(_GOOD, BESTTEAM_DATABASE_URL="mysql+pymysql://u:p@h/d")))["database"]
    assert mysql.level == "FAIL"
    assert "mysql" in mysql.message


def test_check_health_reports_an_unreachable_server_database(monkeypatch, capsys):
    from sqlalchemy.exc import OperationalError
    from ui.backend import admin

    monkeypatch.setenv("BESTTEAM_DATABASE_URL", "postgresql+psycopg://bt:s3cretpw@db.internal:5432/bestteam")
    monkeypatch.delenv("BESTTEAM_DB_PATH", raising=False)

    def _refuse():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(admin, "_open_session", _refuse)
    assert admin.main(["check-health"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] database" in out
    assert "s3cretpw" not in out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_env_check.py -v -k "database_line or unreachable"`
Expected: FAIL — `KeyError: 'database'` / `check-health` crashes with the OperationalError.

- [ ] **Step 3: Implement in `env_check.py`**

Delete `import sqlite3`, `from pathlib import Path` stays (used by `Union[str, Path]`), and delete `from urllib.request import pathname2url`. At the end of `check_environment` (before `return out`) add:

```python
    # --- database -----------------------------------------------------------
    out.append(check_database_url(env))
```

Replace everything from the `# --- schema version` comment block (the `_SCHEMA = "schema"` line, `_DEFAULT_DB_PATH`, `default_db_path`, `_stamped_revision`, `check_schema`, `check_org_retention`, `_MODEL_CATALOG`, `check_model_catalog`) with:

```python
# --- database ------------------------------------------------------------

_DATABASE = "database"
_SUPPORTED_BACKENDS = ("sqlite", "postgresql")


def check_database_url(env: Mapping[str, str]) -> Finding:
    """Which database the backend will open, and whether it can.

    Pure over the environment like the rest of `check_environment`: it parses
    the URL and checks that the driver imports; it never connects.
    `check_schema` is the one that reads.
    """
    try:
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        from .db.database import describe_database_url, resolve_database_url
    except ImportError:
        return Finding("WARN", _DATABASE, "sqlalchemy is not installed, so the database cannot be "
                       "checked (pip install 'bestteam[ui]')")
    url = resolve_database_url(env)
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        return Finding("FAIL", _DATABASE, f"BESTTEAM_DATABASE_URL is not a valid database URL ({exc}). "
                       "Example: postgresql+psycopg://user:password@host:5432/bestteam")
    backend = parsed.get_backend_name()
    if backend not in _SUPPORTED_BACKENDS:
        return Finding("FAIL", _DATABASE, f"BESTTEAM_DATABASE_URL names the {backend!r} engine; "
                       "only sqlite and postgresql are supported")
    if backend != "sqlite":
        try:
            parsed.get_dialect().import_dbapi()
        except (ImportError, ArgumentError) as exc:
            return Finding("FAIL", _DATABASE, f"the {backend} driver is not installed ({exc}); "
                           "install the `ui` extra: pip install 'bestteam[ui]'")
    description = describe_database_url(url)
    if _get(env, "BESTTEAM_DATABASE_URL") and _get(env, "BESTTEAM_DB_PATH"):
        return Finding("WARN", _DATABASE, "both BESTTEAM_DATABASE_URL and BESTTEAM_DB_PATH are set; "
                       f"the URL wins ({description}). Unset BESTTEAM_DB_PATH to avoid confusion")
    return Finding("OK", _DATABASE, description)


def default_database_url(env: Mapping[str, str]) -> str:
    from .db.database import resolve_database_url

    return resolve_database_url(env)


def _as_url(target: Union[str, Path, None]) -> str:
    """Accept the historical file-path argument as well as a URL."""
    from .db.database import resolve_database_url, sqlite_url_for

    if target is None:
        return resolve_database_url({})
    as_text = str(target)
    return as_text if "://" in as_text else sqlite_url_for(as_text)


def _absent_file(url: str) -> bool:
    """True for a SQLite file URL whose file does not exist (nothing to read)."""
    from .db.database import sqlite_path_of

    path = sqlite_path_of(url)
    return path is not None and not path.exists()


def _read_only(url: str):
    from .db.database import readonly_engine

    return readonly_engine(url)


# --- schema version ------------------------------------------------------
#
# Separate from `check_environment`, which is pure over an environment
# mapping and must stay that way. This one reads the database, so it gets
# its own function and its own Finding, and the CLI appends it.

_SCHEMA = "schema"
_DEFAULT_SCRIPT_LOCATION = Path(__file__).resolve().parents[2] / "alembic"


def _stamped_revision(url: str) -> Optional[str]:
    """The database's Alembic revision, or None if it carries no stamp.

    Read-only (`db.database.readonly_engine`), so a checklist run can neither
    create a SQLite file nor write to one that exists --
    `test_check_env_does_not_create_the_database` pins that.
    """
    from sqlalchemy import inspect, text

    engine = _read_only(url)
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                return None
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
    finally:
        engine.dispose()
    return row[0] if row else None


def check_schema(
    target: Union[str, Path, None] = None,
    *,
    script_location: Union[str, Path, None] = None,
) -> Finding:
    """Whether the database's schema matches the migrations in this checkout.

    `init_db` runs `create_all`, which creates missing *tables* and never adds
    a column to a table that already exists. So a database left behind head
    boots clean, serves most of the app, and then raises `no such column` from
    whichever feature touches the new column first -- observed 2026-08-23,
    when a dev database two revisions behind failed an ingestion run rather
    than the launch that should have caught it. Hence FAIL: being behind head
    is not a preference, it is a deployment that will break somewhere.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from .db.database import MEMORY_URL, describe_database_url, sqlite_path_of

    url = _as_url(target)
    if url == MEMORY_URL:
        return Finding("OK", _SCHEMA, "in-memory database; nothing to migrate")
    if _absent_file(url):
        return Finding("OK", _SCHEMA, f"no database at {sqlite_path_of(url)} yet; the first start creates it at "
                       "the current schema. Run `alembic upgrade head` afterwards to stamp it")

    try:
        from alembic.script import ScriptDirectory
        from alembic.script.revision import RevisionError
    except ImportError:
        return Finding("WARN", _SCHEMA, "alembic is not installed, so the schema version cannot be "
                       "checked (pip install 'bestteam[ui]')")

    try:
        stamped = _stamped_revision(url)
    except SQLAlchemyError as exc:
        # A SQLite file that cannot be read is a warning; a server that cannot
        # be reached is what the backend itself will die on -- FAIL.
        level = "WARN" if sqlite_path_of(url) is not None else "FAIL"
        return Finding(level, _SCHEMA, f"could not read the schema version from "
                       f"{describe_database_url(url)}: {str(exc).splitlines()[0]}")

    script = ScriptDirectory(str(script_location or _DEFAULT_SCRIPT_LOCATION))
    head = script.get_current_head()

    if stamped is None:
        # `docs/deployment.md` has the operator start the backend and *then*
        # run `alembic upgrade head`. In between, create_all has built the
        # tables at the current models but nothing stamped them, so the next
        # migration has no floor to measure from.
        return Finding("WARN", _SCHEMA, "the database carries no Alembic stamp; create_all built it "
                       f"but no migration has been recorded. Run `alembic upgrade head` (head is {head})")
    if stamped == head:
        return Finding("OK", _SCHEMA, f"at head ({head})")

    try:
        pending = [rev.revision for rev in script.iterate_revisions(head, stamped)]
    except RevisionError:
        # A database written by a newer checkout than the code being launched.
        return Finding("FAIL", _SCHEMA, f"stamped {stamped}, which is not a revision in this "
                       f"checkout (head is {head}). The database is newer than the code -- deploy the "
                       "matching version rather than migrating")
    return Finding("FAIL", _SCHEMA, f"stamped {stamped}, {len(pending)} migration(s) behind head "
                   f"({head}): {', '.join(reversed(pending))}. The backend will start and then fail "
                   "with `no such column` in whichever feature touches a new column first. "
                   "Run `alembic upgrade head`")


# --- org retention --------------------------------------------------------
#
# BESTTEAM_RUN_RETENTION_DAYS only seeds orgs created after it is set, so the
# env check above can say OK while every existing org still keeps run history
# forever. This one reads the live database (read-only, like check_schema)
# and names those orgs.

_ORG_RETENTION = "org-retention"


def check_org_retention(target: Union[str, Path, None] = None) -> Finding:
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import SQLAlchemyError

    from .db.database import MEMORY_URL, describe_database_url

    url = _as_url(target)
    if url == MEMORY_URL or _absent_file(url):
        return Finding("OK", _ORG_RETENTION, "no database yet; nothing to check")

    try:
        engine = _read_only(url)
        try:
            with engine.connect() as conn:
                tables = inspect(conn)
                if not (tables.has_table("organizations") and tables.has_table("org_retention_settings")):
                    return Finding("OK", _ORG_RETENTION, "pre-migration schema; nothing to check")
                uncovered = [row[0] for row in conn.execute(text(
                    "SELECT o.name FROM organizations o "
                    "LEFT JOIN org_retention_settings r ON r.org_id = o.id "
                    "WHERE r.run_retention_days IS NULL ORDER BY o.name"
                ))]
        finally:
            engine.dispose()
    except SQLAlchemyError as exc:
        return Finding("WARN", _ORG_RETENTION, f"could not read org retention from "
                       f"{describe_database_url(url)}: {str(exc).splitlines()[0]}")

    if uncovered:
        return Finding("WARN", _ORG_RETENTION,
                       f"org(s) keeping run history forever: {', '.join(uncovered)}. "
                       "Set a retention period per org (PUT /api/org/retention) before "
                       "a real customer uses it")
    return Finding("OK", _ORG_RETENTION, "every org has a retention period")


_MODEL_CATALOG = "model-catalog"

# Kept in sync by hand with `db/model_catalog.py::EMBEDDING_TIER` and the
# `fake:`/`fake-architect:` prefixes `adapters/langgraph_adapter.py::_resolve_model`
# understands. This module reads the tables directly rather than importing
# the ORM, the same way `check_org_retention` does.
_EMBEDDING_TIER = "embedding"
_STUB_PREFIXES = ("fake:", "fake-architect:")


def check_model_catalog(target: Union[str, Path, None] = None) -> Finding:
    """WARN when the catalog holds no real chat model.

    The Team Builder wizard runs the Solution Architect on whatever
    `pickDefaultModel()` returns, and that function's last resort is simply
    the first catalog entry. With only stub entries left, that resort picks
    one -- and `fake-architect:` answers the wizard's schemas with a canned
    team, identical for every intent, with no error anywhere. A real
    deployment never seeds a `fake-architect:` entry, but the E2E fixture
    creates exactly this shape if it is ever pointed at a real database
    (observed twice on a dev box), and an admin can delete their way here.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import SQLAlchemyError

    from .db.database import MEMORY_URL, describe_database_url

    url = _as_url(target)
    if url == MEMORY_URL or _absent_file(url):
        return Finding("OK", _MODEL_CATALOG, "no database yet; nothing to check")

    try:
        engine = _read_only(url)
        try:
            with engine.connect() as conn:
                if not inspect(conn).has_table("model_catalog"):
                    return Finding("OK", _MODEL_CATALOG, "pre-migration schema; nothing to check")
                rows = list(conn.execute(text("SELECT spec, tier FROM model_catalog")))
        finally:
            engine.dispose()
    except SQLAlchemyError as exc:
        return Finding("WARN", _MODEL_CATALOG, f"could not read the model catalog from "
                       f"{describe_database_url(url)}: {str(exc).splitlines()[0]}")

    usable = [
        spec for spec, tier in rows
        if tier != _EMBEDDING_TIER and not str(spec).startswith(_STUB_PREFIXES)
    ]
    if usable:
        return Finding("OK", _MODEL_CATALOG, f"{len(usable)} chat model(s) available to the wizard")

    leftover = ", ".join(sorted(str(spec) for spec, _ in rows)) or "the catalog is empty"
    return Finding("WARN", _MODEL_CATALOG,
                   f"no real chat model in the catalog ({leftover}). The Team Builder "
                   "wizard will fall back to a stub entry and build the same canned team "
                   "for every intent, silently. Add a provider model "
                   "(PUT /api/config/model-catalog/<spec>)")
```

Update the module docstring's `check_schema takes the database file` to `check_schema takes the database URL (or the historical file path)`.

- [ ] **Step 4: Implement in `admin.py`**

In the `from .env_check import (...)` block replace `default_db_path,` with `default_database_url,`. Replace the two command handlers:

```python
    if args.command == "check-env":
        # Deliberately before the database is opened (`_open_session` is what
        # imports `db_session`): the checklist must run on a box whose
        # database does not exist yet, and leave it that way.
        url = default_database_url(os.environ)
        findings = check_environment(os.environ) + [
            check_schema(url),
            check_org_retention(url),
            check_model_catalog(url),
        ]
        return _print_findings(findings)

    if args.command == "check-health":
        from sqlalchemy.exc import OperationalError

        from .db.database import describe_database_url, sqlite_path_of

        # Guard before `_open_session`: on a box with no SQLite file yet,
        # opening the session would CREATE it, and a health check must not.
        url = default_database_url(os.environ)
        path = sqlite_path_of(url)
        if path is not None and not path.exists():
            print(f"[OK]   triggers: no database at {path} yet; nothing to monitor")
            return 0
        from .email_trigger import poll_seconds
        from .trigger_metrics import backlog_alert_seconds, collect, evaluate

        try:
            with _open_session() as db:
                metrics = collect(db)
        except OperationalError as exc:
            # A server database that cannot be reached -- the right signal
            # from a cron job is a FAIL line, not a traceback.
            print(f"[FAIL] database: cannot reach {describe_database_url(url)} "
                  f"({str(exc).splitlines()[0]})")
            return 1
        findings = evaluate(
            metrics,
            poll_interval_seconds=poll_seconds(),
            backlog_threshold_seconds=backlog_alert_seconds(),
        )
        return _print_findings(findings)
```

- [ ] **Step 5: Run the tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_env_check.py tests/test_trigger_metrics.py tests/test_admin_cli.py -v`
Expected: all PASS — including `test_check_env_does_not_create_the_database`, `test_the_cli_reports_schema_drift_and_exits_1`, `test_check_health_cli_without_a_database_says_so`. Then confirm the real CLI: `./.venv/Scripts/python.exe -m ui.backend.admin check-env | grep database` prints `[OK]   database: sqlite file ...`.

- [ ] **Step 6: Commit**

```bash
git add ui/backend/env_check.py ui/backend/admin.py tests/test_env_check.py
git commit -m "feat(admin): check-env and check-health follow BESTTEAM_DATABASE_URL and name the database"
```

### Task 8: ship the Postgres driver (spec §7, Ruling 5)

**Files:**
- Modify: `pyproject.toml:18`
- Regenerate: `requirements.lock`
- Test: `tests/test_database_url.py`, `tests/test_packaging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_database_url.py`:

```python
def test_the_postgres_driver_ships_with_the_ui_extra():
    """`psycopg[binary]` is in the `ui` extra so the same image serves either engine (Ruling 5)."""
    from sqlalchemy.engine import make_url

    make_url("postgresql+psycopg://u:p@h/d").get_dialect().import_dbapi()
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_database_url.py -v -k driver` → FAIL (`No module named 'psycopg'`).

- [ ] **Step 2: Add the dependency and regenerate the lockfile**

In `pyproject.toml` line 18 append `, "psycopg[binary]>=3.1"` inside the `ui` list. Then:

```bash
uv pip compile pyproject.toml --universal --python-version 3.10 --extra ui --extra dev --extra tools --extra test --extra interview --extra providers-openai --extra providers-deepseek --extra providers-google -o requirements.lock
./.venv/Scripts/python.exe -m pip install -c requirements.lock -e ".[ui,dev,tools,test]"
./.venv/Scripts/python.exe -c "import psycopg; print(psycopg.__version__)"
git diff --stat requirements.lock
```

Expected: the lock diff adds `psycopg` and `psycopg-binary` lines only (plus their version pins); nothing else moves.

- [ ] **Step 3: Run the tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_database_url.py tests/test_packaging.py -v`
Expected: PASS (if `test_packaging.py` fails on the pre-existing GBK encoding issue noted in memory, that failure is unrelated — say so in the PR).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml requirements.lock tests/test_database_url.py
git commit -m "build: ship psycopg in the ui extra so one image serves SQLite or Postgres"
```

### Task 9: documentation and the decision record (spec §12)

**Files:**
- Modify: `docs/DECISIONS.md` (append), `docs/deployment.md:112-175,263-275`, `.env.example:46`, `ui/backend/db/CLAUDE.md:10-24`, `ui/backend/CLAUDE.md` (email-trigger section), `docs/ADMIN_GUIDE.md:216-222`

- [ ] **Step 1: DECISIONS.md — append this entry at the end of the file**

```markdown
## Postgres is the target engine; the code supports both; SQLite stays in production until the cutover trigger

- **Status**: Accepted (2026-09-07). Extends, and does not overturn, "Beta
  runs single-process on SQLite" above: production is still one process on
  one SQLite file.
- **Context**: Asked what "scalability" means for the platform, the owner
  named all four readings — more organisations and load, more than one host,
  data that outside tools (reporting, a second product) can reach, and a
  team larger than one. A SQLite file has no network interface and lives on
  one host, so the last three are structurally out of its reach: Postgres is
  a matter of *when*, not *whether*. The entry above lists only benefit-side
  triggers; it had no cost-side one, and a cutover's cost rises with every
  customer whose data is in the file (a rehearsal, a rollback plan, an
  announced window). The live database held no customer organisation on
  2026-09-07.
- **Decision**: split the work. **Now, the code half** (spec
  `docs/superpowers/specs/2026-09-07-database-engine-portability-design.md`):
  one connection URL (`BESTTEAM_DATABASE_URL`, else the legacy
  `BESTTEAM_DB_PATH`) selects the engine; the schema, queries, Alembic,
  `check-env` and the operator CLI are dialect-neutral for sqlite and
  postgresql; a CI lane runs the backend suite against Postgres on every
  backend PR; `admin migrate-db` copies a database into an empty one with
  pre-flight checks. **Later, the ops half** — provisioning, backup/restore,
  runbooks, rehearsal, cutover — as its own spec when the trigger fires.
- **Cutover trigger**: the first quiet week after all three first customers
  are live and have run for two weeks, or 2026-12-01, whichever comes first.
  Hitting it means writing the ops-half spec, not re-opening this entry.
- **Consequences**:
  - "Supports both" means CI-verified, not operated: nothing in production
    runs Postgres until the ops half lands. Local development and the test
    suite's default stay on SQLite — a new developer needs no database
    server; Postgres exists only in CI and, later, on the live server.
  - Every migration and query from here on must pass the Postgres lane. The
    differences that matter most: foreign keys (enforced by Postgres, never
    on the production SQLite file, and enforced in the *test* engine so the
    SQLite suite surfaces the same defects) and concurrent writers under
    `READ COMMITTED` (today's process-level locks cover every known
    check-then-act; ADR 2 owns this properly).
  - The roadmap behind this: **ADR 2, shared state** (`RunRegistry`, the
    dispatch and idempotency locks, the login throttle, the WebSocket tickets
    out of the process — what multi-host actually costs) and **ADR 3, data
    platform** (a read-only reporting role, row-level security on `org_id`,
    JSONB). Postgres is a precondition for both, not a substitute.
  - The per-user memory store and the vector knowledge-base files are not
    covered: a Postgres deployment still needs file backups for them.
```

- [ ] **Step 2: deployment.md**

In §1, insert before the paragraph starting `TLS termination`:

```markdown
- **Database engine.** By default the backend uses a SQLite file on the data
  volume (`ui/backend/data/bestteam.db`, or `BESTTEAM_DB_PATH`). Setting
  `BESTTEAM_DATABASE_URL` (for example
  `postgresql+psycopg://user:password@host:5432/bestteam`) selects a server
  database instead; the URL wins when both are set, and Alembic, the operator
  CLI and `check-env` all follow the same setting. **SQLite is the supported
  production engine today.** Postgres support is code-complete and verified
  in CI on every change, but it is not yet operated in production — the
  runbook, backup and cutover procedure for it come with the ops half of
  `docs/superpowers/specs/2026-09-07-database-engine-portability-design.md`.
  `check-env` prints which database it resolved to (`[OK] database: ...`).
```

In §3, after the first paragraph, add: `The migration runs against whatever \`BESTTEAM_DATABASE_URL\` / \`BESTTEAM_DB_PATH\` resolves to — the same database the server opens.`

- [ ] **Step 3: `.env.example`**

Insert before the `# Per-user memory (optional, off by default).` comment block:

```
# The deployment database. Leave unset for the default SQLite file on the data
# volume (ui/backend/data/bestteam.db; BESTTEAM_DB_PATH overrides the path).
# A server database is selected with a SQLAlchemy URL, e.g.
#   BESTTEAM_DATABASE_URL=postgresql+psycopg://user:password@host:5432/bestteam
# SQLite is the supported production engine today; see docs/deployment.md
# section 1, "Database engine". `check-env` reports which one is in use.
BESTTEAM_DATABASE_URL=

```

- [ ] **Step 4: `ui/backend/db/CLAUDE.md` — replace the "Engine and wiring" section**

```markdown
## Engine and wiring

`db/database.py`: `resolve_database_url(env)` (`BESTTEAM_DATABASE_URL` wins,
else `BESTTEAM_DB_PATH` → `sqlite:///…`, default `ui/backend/data/bestteam.db`),
`make_engine(path | ":memory:" | url)`, `init_db(engine)`, `session_factory`,
`readonly_engine(url)` (a SQLite file via `mode=ro`, so a check can never
create it). `ui/backend/db_session.py` wires the per-deployment engine, the
`get_db()` dependency and `LOCK_ANCHOR` (what the single-instance lock is keyed
on). **sqlite and postgresql are the two supported engines**; Postgres is
CI-verified, not yet operated — `docs/DECISIONS.md`.

- `":memory:"` uses a `StaticPool` so all connections share one database —
  needed for tests and dry runs.
- A SQLite file engine sets **`PRAGMA journal_mode=WAL`** on every connection,
  so readers aren't blocked by the one writer the run workers / ingestion /
  poller / requests take turns being. ⚠️ The `-wal`/`-shm` siblings are why
  `scripts/backup-db.sh` goes through the **online backup API**, not a file copy.
- **SQLite foreign-key enforcement is off in production** — several notes
  below depend on knowing that nothing catches a dangling FK for you there.
  Postgres always enforces, so parents must be written before children.
- Dialect-specific spots, all deliberate: `inbox_events._insert_for` picks the
  sqlite/postgresql `insert` for `on_conflict_do_nothing`; the users partial
  index carries both `sqlite_where` and `postgresql_where`; boolean server
  defaults are `true()`/`false()`, never `text("1")` (Postgres rejects an
  integer default on a boolean).
```

Also, in the first paragraph of that file, `Per-deployment SQLite via SQLAlchemy 2.0` → `Per-deployment SQLite (or Postgres by URL) via SQLAlchemy 2.0`.

- [ ] **Step 5: `ui/backend/CLAUDE.md`**

In the "Autonomous email trigger" section, replace
`` `_lifespan` takes an exclusive OS lock on `<db>.lock` (`process_lock.py`) ``
with
`` `_lifespan` takes an exclusive OS lock on `<db>.lock` beside the SQLite file, or `data/bestteam.lock` for a server database (`process_lock.py`, `db_session.LOCK_ANCHOR`) ``
and replace
`Real scale-out is blocked on Postgres (`make_engine` hardcodes SQLite and takes a path, not a URL).`
with
`Real scale-out is blocked on this in-process state, not on the engine (`BESTTEAM_DATABASE_URL` can already name Postgres; `docs/DECISIONS.md`, 2026-09-07).`

- [ ] **Step 6: `docs/ADMIN_GUIDE.md`** — in the §7 table add a row after `BESTTEAM_DB_PATH`:

```markdown
| `BESTTEAM_DATABASE_URL` | A server database (Postgres) instead of the SQLite file; wins over `BESTTEAM_DB_PATH`. Not operated in production yet. |
```

- [ ] **Step 7: Verify and commit**

Run: `grep -rn "hardcodes SQLite" ui docs CLAUDE.md` → nothing; `grep -n "BESTTEAM_DATABASE_URL" .env.example docs/deployment.md docs/ADMIN_GUIDE.md ui/backend/db/CLAUDE.md` → one or more hits each.

```bash
git add docs/DECISIONS.md docs/deployment.md .env.example ui/backend/db/CLAUDE.md ui/backend/CLAUDE.md docs/ADMIN_GUIDE.md
git commit -m "docs: record the Postgres-target decision, the cutover trigger and BESTTEAM_DATABASE_URL"
```

### Task 10: measure SQL per request, record status, run the gates, open PR 1 (spec §12, §14)

**Files:**
- Create (scratchpad only, not committed): `<scratchpad>/count_sql.py`
- Modify: `docs/STATUS.md` (top of `## Done`)

- [ ] **Step 1: Measure**

Write `<scratchpad>/count_sql.py` (the scratchpad path is in the session's system prompt):

```python
"""How many SQL statements three customer-facing requests issue against a copy
of the development database. One-off (spec §12); the numbers go to STATUS.md."""
import os
import shutil
import sys
from pathlib import Path

copy = Path(sys.argv[1]) / "measure.db"
shutil.copy(Path("ui/backend/data/bestteam.db"), copy)
os.environ["BESTTEAM_DB_PATH"] = str(copy)
os.environ.setdefault("BESTTEAM_SECRET_KEY", "measure-" + "x" * 48)

from fastapi.testclient import TestClient
from sqlalchemy import event

from ui.backend import main as backend_main
from ui.backend.auth_api import get_current_org, get_current_user
from ui.backend.db.models import Organization, Run, User
from ui.backend.db_session import SessionLocal, engine

count = {"n": 0}


@event.listens_for(engine, "before_cursor_execute")
def _count(conn, cursor, statement, parameters, context, executemany):
    count["n"] += 1


with SessionLocal() as db:
    org = db.query(Organization).filter_by(name="default").one()
    user = db.query(User).filter_by(org_id=org.id).first()
    run = db.query(Run).filter_by(org_id=org.id).order_by(Run.created_at.desc()).first()

backend_main.app.dependency_overrides[get_current_user] = lambda: user
backend_main.app.dependency_overrides[get_current_org] = lambda: org
client = TestClient(backend_main.app)
for label, path in [
    ("run list", "/api/runs"),
    ("run detail", f"/api/runs/{run.id}/trace"),
    ("KB list", "/api/org/knowledge-bases"),
]:
    count["n"] = 0
    response = client.get(path)
    print(f"{label}: HTTP {response.status_code}, {count['n']} SQL statements")
```

Run it with the same environment the dev backend uses (the `.env` keys, because the dev database holds stored mailbox credentials that `main` verifies at import):

```powershell
.\.venv\Scripts\python.exe <scratchpad>\count_sql.py <scratchpad>
```

Expected: three lines, each `HTTP 200`. If a request returns 4xx, fix the override (the endpoint's dependency name) rather than skipping it.

- [ ] **Step 2: STATUS.md**

Insert at the top of `## Done` (before the 2026-09-06 entry):

```markdown
- **The database is chosen by one URL; the code is Postgres-ready, production
  stays on SQLite** (2026-09-07, PR 1 of 3 for
  `specs/2026-09-07-database-engine-portability-design.md`).
  `BESTTEAM_DATABASE_URL` (else the legacy `BESTTEAM_DB_PATH`) feeds
  `db.database.resolve_database_url`, which `db_session`, `alembic/env.py`,
  `check-env`/`check-health` and the instance lock all follow; `make_engine`
  takes a path, `:memory:` or a URL. The four SQLite-only spots are gone:
  boolean `DEFAULT 1` in two models and four migrations (Postgres rejects it
  on a boolean), the users partial index declared for SQLite only, the dedup
  insert bound to the SQLite dialect, and `check-env` opening the file with
  stdlib `sqlite3`. `psycopg` ships in the `ui` extra. Nothing in production
  changes; the cutover trigger is in `DECISIONS.md`. Measured once for the
  ops half: SQL statements per request against a copy of the dev database —
  run list <N1>, run detail <N2>, KB list <N3>. PR 2 adds the Postgres CI lane
  and foreign-key enforcement in the test engine; PR 3 adds `admin migrate-db`.
```

Replace `<N1>`, `<N2>`, `<N3>` with the measured numbers.

- [ ] **Step 3: Run the local gates**

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not e2e" -n auto
.\.venv\Scripts\python.exe -m pytest tests/e2e -m "e2e and not slow"
```

Expected: all green (2,437+ tests). The frontend is untouched, so its lint/build gates do not apply. If the e2e tier fails on the known 100 %-CPU import timeout rather than on an assertion, rerun it once with nothing else running.

- [ ] **Step 4: Commit, push, open PR 1**

```bash
git add docs/STATUS.md
git commit -m "docs(status): database engine portability, PR 1 of 3"
git push -u origin feat/db-engine-portability
gh pr create --base main --title "Database engine portability (1/3): one URL, dialect-neutral code, driver in the image" --body-file <scratchpad>/pr1.md
```

`pr1.md`: three paragraphs — what changed (the STATUS entry, reworded), what did not (production, local dev, tests still SQLite), how it was verified (the two gate commands and the smoke boot from Task 3), the measured numbers, and the trailer `🤖 Generated with [Claude Code](https://claude.com/claude-code)` plus the session link. Then watch CI: `gh pr checks --watch`.

---

## PR 2 — the test engine, foreign keys, the Postgres lane

Cut `feat/db-engine-portability-tests` from the head of PR 1 (`git checkout -b feat/db-engine-portability-tests feat/db-engine-portability`). Open it as a **draft** PR against `main` as soon as Task 15 lands, so the `backend-postgres` job runs on every push; re-target after PR 1 merges.

### Task 11: one test-engine entry point, per-test Postgres databases, the `sqlite_only` marker (spec §5, §8)

**Files:**
- Create: `tests/_postgres.py`
- Modify: `tests/helpers.py:28-85` (replace `make_concurrent_safe_engine`)
- Modify: `tests/conftest.py` (two fixtures/hooks)
- Modify: `pyproject.toml:47-53` (marker)
- Test: `tests/test_test_engine.py`

**Interfaces:**
- Produces `tests/helpers.make_test_engine(tmp_path: Path | None = None) -> Engine`.
- Produces `tests/_postgres.py`: `enabled() -> bool`, `url_for(database: str) -> URL`, `ensure_template() -> str`, `clone_engine() -> Engine`, `empty_database_url() -> URL`, `drop_created() -> None`, `drop_template() -> None`. Reads `BESTTEAM_TEST_DATABASE_URL` once at import.
- Removes `make_concurrent_safe_engine` (Task 12 rewrites every caller).

- [ ] **Step 1: Write the failing test**

Create `tests/test_test_engine.py`:

```python
"""The suite's engine helper: foreign keys enforced in every shape (spec §5, §8)."""

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")

from sqlalchemy.exc import IntegrityError

from helpers import make_test_engine
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import TraceEventRecord


@pytest.mark.parametrize("use_file", [False, True], ids=["memory", "file"])
def test_the_test_engine_enforces_foreign_keys(tmp_path, use_file):
    engine = make_test_engine(tmp_path if use_file else None)
    init_db(engine)
    try:
        with session_factory(engine)() as db:
            db.add(TraceEventRecord(run_id="no-such-run", seq=0, type="run_failed"))
            with pytest.raises(IntegrityError):
                db.commit()
    finally:
        engine.dispose()
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_test_engine.py -v` → FAIL, `cannot import name 'make_test_engine'`.

- [ ] **Step 2: Create `tests/_postgres.py`**

```python
"""Postgres support for the test suite (spec §8).

Off unless `BESTTEAM_TEST_DATABASE_URL` is set to a *maintenance* URL -- a
database the test user may connect to while creating and dropping others,
e.g. `postgresql+psycopg://postgres:postgres@localhost:5432/postgres`. With
it set, `tests/helpers.py::make_test_engine` hands every caller a fresh
database cloned from a per-session template, and `tests/conftest.py` drops
each test's databases when the test ends (a clone is several megabytes; a
session would otherwise leave more than a thousand behind).

Not a test module (no `test_` prefix), like `tests/e2e/_guard.py`.
"""

from __future__ import annotations

import os
import uuid
from typing import List, Optional, Tuple

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

MAINTENANCE_URL = os.environ.get("BESTTEAM_TEST_DATABASE_URL", "").strip()

_template: Optional[str] = None
# (engine, database name) created since the last drop_created(); conftest
# drains it after every test.
_created: List[Tuple[Engine, str]] = []


def enabled() -> bool:
    return bool(MAINTENANCE_URL)


def url_for(database: str) -> URL:
    return make_url(MAINTENANCE_URL).set(database=database)


def _admin() -> Engine:
    # CREATE/DROP DATABASE cannot run inside a transaction: autocommit, and no
    # pool so nothing lingers connected to a database about to be dropped.
    return create_engine(MAINTENANCE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)


def _create(name: str, template: Optional[str] = None) -> None:
    admin = _admin()
    try:
        with admin.connect() as conn:
            suffix = f' TEMPLATE "{template}"' if template else ""
            conn.execute(text(f'CREATE DATABASE "{name}"{suffix}'))
    finally:
        admin.dispose()


def _drop(name: str) -> None:
    admin = _admin()
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def ensure_template() -> str:
    """The session's template database: the current `create_all` schema, no rows."""
    global _template
    if _template is None:
        name = f"bestteam_tmpl_{uuid.uuid4().hex[:8]}"
        _create(name)
        engine = create_engine(url_for(name))
        try:
            from ui.backend.db import init_db

            init_db(engine)
        finally:
            # CREATE DATABASE ... TEMPLATE refuses while anyone is connected.
            engine.dispose()
        _template = name
    return _template


def clone_engine() -> Engine:
    """A fresh database with the schema and no rows, dropped after the test."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    _create(name, template=ensure_template())
    engine = create_engine(url_for(name), pool_pre_ping=True)
    _created.append((engine, name))
    return engine


def empty_database_url() -> URL:
    """A brand-new database with NO schema (migration replay), dropped after the test."""
    name = f"e_{uuid.uuid4().hex[:12]}"
    _create(name)
    _created.append((create_engine(url_for(name), poolclass=NullPool), name))
    return url_for(name)


def drop_created() -> None:
    while _created:
        engine, name = _created.pop()
        engine.dispose()
        _drop(name)


def drop_template() -> None:
    global _template
    if _template is not None:
        _drop(_template)
        _template = None
```

- [ ] **Step 3: Replace `make_concurrent_safe_engine` in `tests/helpers.py`**

Replace the whole function (lines 28–85) with:

```python
def make_test_engine(tmp_path: Optional[Path] = None):
    """The one engine every test fixture uses.

    Default: SQLite. With no `tmp_path`, an in-memory database on a
    `StaticPool` -- ONE DBAPI connection backs every Session, so two
    concurrent Sessions share a single transaction and whoever commits or
    rolls back first does it for both:

        T1 (request A)                  T2 (request B / worker thread)
        --------------------------      -------------------------------------
        flush()  -> UPDATE/DELETE
                                        close() / pool check-in -> ROLLBACK
                                          ... discards A's flushed statements
        commit() -> COMMIT (no-op)
          -> endpoint answers 200/204 having written nothing

    That failure is silent, so it surfaces as an unrelated assertion failing
    much later, intermittently. Fine for a test with one live Session; wrong
    for any test where a request overlaps another request, a run worker or an
    ingestion thread. Those pass `tmp_path` and get a file-backed database
    whose `QueuePool` gives each Session its own connection, as production
    does. The file lives in its own subdirectory because callers reuse
    `tmp_path` for pipelines, sessions and uploads; fsync is off because the
    database dies with `tmp_path`.

    Both SQLite shapes enforce foreign keys (`PRAGMA foreign_keys=ON`) -- the
    production file does not, Postgres always does, and the suite is where a
    child-before-parent write should be caught (spec 2026-09-07 §5).

    With `BESTTEAM_TEST_DATABASE_URL` set (the `backend-postgres` CI lane, or
    a local server), either shape returns a fresh Postgres database cloned
    from a per-session template; `tests/conftest.py` drops it when the test
    ends. A test that depends on the `:memory:` shared-connection behaviour
    on purpose (`test_email_trigger.py` commits through a second Session
    while the first holds an uncommitted write) is marked `sqlite_only` and
    skipped there.
    """
    if _postgres.enabled():
        return _postgres.clone_engine()
    if tmp_path is None:
        engine = make_engine(":memory:")
    else:
        db_dir = tmp_path / "_db"
        db_dir.mkdir(exist_ok=True)
        engine = make_engine(db_dir / "test.db")

        @event.listens_for(engine, "connect")
        def _no_fsync(dbapi_connection, _record):
            # Pay for the isolation above, not for durability: `synchronous`
            # governs only when writes reach the platter.
            dbapi_connection.execute("PRAGMA synchronous=OFF")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine
```

Add `import _postgres` after the `from sqlalchemy import event` import (the `tests/` directory is on `sys.path`, which is how `from helpers import ...` already works).

- [ ] **Step 4: conftest hooks**

Append to `tests/conftest.py`:

```python
@_pytest.fixture(autouse=True)
def _drop_postgres_test_databases():
    """Spec §8: on the Postgres lane every database a test created is dropped
    when the test ends -- per test, because a template clone is several
    megabytes and a session creates more than a thousand of them."""
    yield
    try:
        import _postgres
    except ImportError:  # pragma: no cover - SDK-only checkout without sqlalchemy
        return
    if _postgres.enabled():
        _postgres.drop_created()


def pytest_sessionfinish(session, exitstatus):
    try:
        import _postgres
    except ImportError:  # pragma: no cover
        return
    if _postgres.enabled():
        _postgres.drop_created()
        _postgres.drop_template()
```

And at the end of the existing `pytest_collection_modifyitems` (after the `setattr(...)` call):

```python
    try:
        import _postgres
    except ImportError:  # pragma: no cover
        return
    if _postgres.enabled():
        skip = _pytest.mark.skip(reason="depends on SQLite's :memory: shared connection; not meaningful on Postgres")
        for item in items:
            if item.get_closest_marker("sqlite_only"):
                item.add_marker(skip)
```

In `pyproject.toml`, add to the `markers` list:

```
    "sqlite_only: depends on SQLite's in-memory shared connection on purpose; skipped on the Postgres lane",
```

- [ ] **Step 5: Run the new test and the helper's neighbours**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_test_engine.py -v`
Expected: both cases PASS. (`tests/test_builder_api.py` and friends still import `make_concurrent_safe_engine` and will fail to import until Task 12 — expected; do not run the full suite yet.)

- [ ] **Step 6: Commit**

```bash
git add tests/_postgres.py tests/helpers.py tests/conftest.py tests/test_test_engine.py pyproject.toml
git commit -m "test: one make_test_engine entry point with foreign keys on, and per-test Postgres clones"
```

### Task 12: the sweep — every fixture goes through `make_test_engine` (spec §8)

**Files:**
- Modify: every `tests/test_*.py` calling `make_engine(":memory:")` (71 sites, 50 files) or `make_concurrent_safe_engine(...)` (50 sites, 25 files)
- Create (scratchpad only): `<scratchpad>/sweep_test_engines.py`

- [ ] **Step 1: Run the mechanical sweep**

```python
"""Route every test fixture through helpers.make_test_engine (spec §8)."""
import re
from pathlib import Path

for path in sorted(Path("tests").glob("test_*.py")):
    text = path.read_text(encoding="utf-8")
    original = text
    text = text.replace('make_engine(":memory:")', "make_test_engine()")
    text = re.sub(r"make_concurrent_safe_engine\((\w+)\)", r"make_test_engine(\1)", text)
    text = text.replace("make_concurrent_safe_engine", "make_test_engine")
    if text == original:
        continue
    # Import: extend an existing `from helpers import ...` line, else add one
    # just above the first `from ui.backend...` import.
    helpers_import = re.search(r"from helpers import ([^\n]+)\n", text)
    if helpers_import is None:
        anchor = re.search(r"from ui\.backend[^\n]*\n", text)
        text = text.replace(anchor.group(0), "from helpers import make_test_engine\n" + anchor.group(0), 1)
    elif "make_test_engine" not in helpers_import.group(1):
        names = sorted({n.strip() for n in helpers_import.group(1).split(",")} | {"make_test_engine"})
        text = text.replace(helpers_import.group(0), "from helpers import " + ", ".join(names) + "\n")
    # Drop `make_engine` from the ui.backend.db import when it is no longer called.
    if "make_engine(" not in text:
        text = re.sub(r"from ui\.backend\.db import make_engine\n", "", text)
        text = re.sub(r"(from ui\.backend\.db import [^\n]*?)make_engine, ", r"\1", text)
        text = re.sub(r"(from ui\.backend\.db import [^\n]*?), make_engine", r"\1", text)
    path.write_text(text, encoding="utf-8")
    print("rewrote", path)
```

Run: `./.venv/Scripts/python.exe <scratchpad>/sweep_test_engines.py`, then:

```bash
grep -rn "make_concurrent_safe_engine" tests | wc -l          # must be 0
grep -rln 'make_engine(":memory:")' tests | wc -l             # must be 0
grep -rn "^from helpers import.*make_test_engine" tests | wc -l
./.venv/Scripts/python.exe -m pytest tests --collect-only -q 2>&1 | tail -3   # no import errors
```

Files that still call `make_engine(` (with a file path) after the sweep are the migration, env-check and CLI tests that build SQLite files on purpose — leave them.

- [ ] **Step 2: Run the whole SQLite suite with foreign keys now enforced**

Run: `./.venv/Scripts/python.exe -m pytest -m "not e2e" -n auto -q 2>&1 | tail -40`

Every failure is one of three kinds; classify each before touching it:

1. **A fixture writes a child before its parent** (e.g. a `Run` with `org_id=999`, a `TraceEventRecord` for a run never inserted, a `pipeline_version_id` pointing nowhere). Fix the fixture: insert the parent, or use the real helper that does (`get_or_create_org`, `create_user`, `publish_pipeline_version`). Commit these in batches by test file.
2. **Product code writes a child before its parent.** This is a defect the spec exists to find. Fix the product code, add a regression test in the same file, commit separately with `fix(...)`.
3. **A test depends on the `:memory:` shared-connection behaviour on purpose** (its comment or the helper docstring says so). Add `@pytest.mark.sqlite_only` with a one-line reason. Expect only `test_email_trigger.py`'s concurrent-writer simulation.

**Fallback agreed in Ruling 7:** if kind 1 exceeds what one session can clean up (as a guide, more than ~40 failing tests that are pure fixture plumbing), remove the `_enforce_foreign_keys` listener from `make_test_engine`, note it in the commit message and in `tests/helpers.py`'s docstring, and let the Postgres lane (Task 16) be the only enforcer. Do **not** silently drop it for fewer.

- [ ] **Step 3: Commit**

```bash
git add tests
git commit -m "test: route every fixture through make_test_engine; foreign keys enforced in the SQLite suite"
```

(Plus the separate `fix(...)` commits from kind 2, if any.)

### Task 13: the failure path writes the run row before its terminal trace (spec §5)

**Files:**
- Modify: `ui/backend/runtime.py:1148-1180`
- Test: `tests/test_runtime_failure_path.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_runtime_failure_path.py`:

```python
"""A run whose row could not be written up front still ends with exactly one
`runs` row and a `run_failed` trace event that points at it (spec §5). The
three 2026-09-05 orphans in the dev database were this path."""

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")

from sqlalchemy import event

from bestteam import AgentSpec, PipelineSpec, Specification, TeamSpec, validate_specification
from helpers import make_test_engine
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import Run, TraceEventRecord
from ui.backend.runtime import registry, run_in_background


def _pipeline(tmp_path):
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:done")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    return validate_specification(spec, source=tmp_path / "w.yaml")


def test_terminal_trace_is_never_written_without_its_run_row(tmp_path):
    engine = make_test_engine(tmp_path)  # foreign keys enforced
    init_db(engine)
    state = {"tripped": False}

    @event.listens_for(engine, "before_cursor_execute")
    def _fail_the_first_read_of_the_runs_row(conn, cursor, statement, parameters, context, executemany):
        # The worker could not read/write its `runs` row and then still had
        # to publish a terminal event. Only the first SELECT on `runs` fails.
        if not state["tripped"] and statement.lstrip().upper().startswith("SELECT") and "runs" in statement:
            state["tripped"] = True
            raise RuntimeError("simulated: runs row unreadable")

    run = registry.create("w", "in")
    run_in_background(run.id, _pipeline(tmp_path), "in", engine=engine)

    assert state["tripped"]
    try:
        with session_factory(engine)() as db:
            rows = db.query(Run).filter_by(id=run.id).all()
            assert [r.status for r in rows] == ["failed"]
            events = db.query(TraceEventRecord).filter_by(run_id=run.id).order_by(TraceEventRecord.seq).all()
            assert [e.type for e in events] == ["run_failed"]
    finally:
        engine.dispose()
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_runtime_failure_path.py -v`
Expected: FAIL — `[] == ["failed"]` (no run row; with foreign keys on, the trace insert was refused and swallowed, so no trace row either).

- [ ] **Step 2: Fix the failure path in `runtime.py`**

In the `except Exception as exc:` block of `run_in_background` (around lines 1148–1180), replace from `if db is not None and run_row is not None:` through the `_safe_record_trace_event(...)` call with:

```python
            row_persisted = False
            if db is not None:
                try:
                    db.rollback()
                    if run_row is None:
                        # The up-front insert never happened -- the failure
                        # was in reading or writing the row itself. The
                        # terminal trace row below needs a parent (Postgres
                        # enforces the foreign key; the test engine does too),
                        # so write the row now, as failed, before the event.
                        run_row = db.get(Run, run_id) or Run(
                            id=run_id,
                            pipeline=getattr(pipeline, "name", ""),
                            input=input,
                            org_id=org_id,
                            username=username,
                            pipeline_version_id=pipeline_version_id,
                        )
                    run_row.status = "failed"
                    run_row.output = message
                    # Operator-only copy, same reason as the stream loop's
                    # sanitizer: `message` says nothing an admin can act on.
                    run_row.internal_error = f"{type(exc).__name__}: {exc}"
                    db.add(run_row)
                    db.commit()
                    row_persisted = True
                    # A triggered run that fails before pipeline.stream() ever
                    # yields an event (e.g. a compile failure) previously
                    # skipped normalization entirely, so its UID batch just
                    # disappeared from Needs-attention instead of getting the
                    # synthetic error rows spec 10.1 requires (Codex review
                    # finding).
                    _maybe_normalize()
                    _maybe_record_share_reply(None)
                except Exception:  # noqa: BLE001
                    _logger.warning("Could not persist failed status for run %s", run_id)
            registry.publish(run_id, dataclasses.asdict(failed_event))
            if db is not None and row_persisted:
                # `_safe_record_trace_event` rolls back internally on failure, so
                # it self-heals a session left poisoned by whatever raised above.
                _safe_record_trace_event(db, run_id=run_id, seq=seq, event=failed_event)
            elif db is not None:
                _logger.warning(
                    "Run %s: its row could not be written, so its run_failed trace "
                    "event is not persisted either (it would have no parent)", run_id,
                )
```

Keep the existing comment block above it ("Persist status + normalize BEFORE publish/trace-record…") in place.

- [ ] **Step 3: Run the tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_runtime_failure_path.py tests/test_runtime_run_row.py tests/test_trace_persistence.py tests/test_email_trigger.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add ui/backend/runtime.py tests/test_runtime_failure_path.py
git commit -m "fix(runtime): write the failed run row before its terminal trace event"
```

### Task 14: the migration chain replays on Postgres and matches `create_all` (spec §8, Ruling 6)

**Files:**
- Create: `tests/test_migrations_postgres.py`

Verified on 2026-09-07 that on SQLite the replayed chain and `create_all` produce identical tables and columns, so equality is the right assertion.

- [ ] **Step 1: Write the test**

```python
"""The Alembic chain replays on Postgres and lands on the same schema as
`create_all` (spec §8, Ruling 6). Runs only on the Postgres lane."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

import _postgres

if not _postgres.enabled():
    pytest.skip("BESTTEAM_TEST_DATABASE_URL is not set", allow_module_level=True)

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from ui.backend.db import init_db

_ROOT = Path(__file__).resolve().parent.parent


def _columns(engine):
    inspector = sa.inspect(engine)
    return {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in inspector.get_table_names()
        if table != "alembic_version"
    }


def test_upgrade_head_on_postgres_matches_create_all():
    migrated = _postgres.empty_database_url()
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", migrated.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, "head")  # must not raise: 41 migrations on a dialect they never ran on

    fresh = _postgres.empty_database_url()
    fresh_engine = sa.create_engine(fresh)
    migrated_engine = sa.create_engine(migrated)
    try:
        init_db(fresh_engine)
        assert _columns(migrated_engine) == _columns(fresh_engine)
    finally:
        fresh_engine.dispose()
        migrated_engine.dispose()
```

- [ ] **Step 2: Run it without a server (skips) and commit**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrations_postgres.py -v` → `SKIPPED (BESTTEAM_TEST_DATABASE_URL is not set)`.

```bash
git add tests/test_migrations_postgres.py
git commit -m "test: replay the migration chain on Postgres and compare it with create_all"
```

It runs for real in Task 16.

### Task 15: the `backend-postgres` CI job (spec §9, Ruling 2)

**Files:**
- Modify: `.github/workflows/ci.yml` (after the `backend-optional-deps` job)

- [ ] **Step 1: Add the job**

```yaml
  backend-postgres:
    needs: changes
    if: needs.changes.outputs.backend == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 25
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
          # C collation, like SQLite's byte order and the local portable server;
          # the ops half checks order-sensitive endpoints on the real server's locale.
          POSTGRES_INITDB_ARGS: "--locale=C --encoding=UTF8"
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      BESTTEAM_TEST_DATABASE_URL: postgresql+psycopg://postgres:postgres@localhost:5432/postgres
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.11"
          cache: "pip"
          cache-dependency-path: requirements.lock
      - name: Install dependencies
        run: pip install -c requirements.lock -e ".[ui,dev,tools,interview]"
      # Serial on purpose: every database-using test clones its own database
      # from a template and drops it afterwards. This lane exists for dialect
      # coverage, not wall clock -- backend-unit-integration keeps -n auto.
      - name: Run pytest against Postgres
        run: python -m pytest -m "not e2e and not optional"
```

- [ ] **Step 2: Validate the YAML and commit**

Run: `./.venv/Scripts/python.exe -c "import yaml, sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ok')"`

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run the backend suite against Postgres on every backend change"
git push -u origin feat/db-engine-portability-tests
gh pr create --draft --base main --title "Database engine portability (2/3): test engine, foreign keys, Postgres lane" --body "Draft while the lane is being made green; see the spec §5, §8–§10."
```

Watch the first run: `gh pr checks --watch`. Expect failures — that is what Task 16 triages.

### Task 16: a local Postgres, the lane, and its triage (spec §10, Ruling 4)

**Files:**
- Scratchpad only for the server; fixes land wherever the failures are.

- [ ] **Step 1: Stand up the portable server (PowerShell)**

```powershell
$scr = "<scratchpad>\pg"
New-Item -ItemType Directory -Force $scr | Out-Null
Invoke-WebRequest -Uri "https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip" -OutFile "$scr\pg.zip"
Expand-Archive "$scr\pg.zip" -DestinationPath $scr -Force
& "$scr\pgsql\bin\initdb.exe" -D "$scr\pgdata" -U postgres --auth=trust -E UTF8 --locale=C
& "$scr\pgsql\bin\pg_ctl.exe" -D "$scr\pgdata" -o "-p 55432 -c listen_addresses=127.0.0.1" -l "$scr\pg.log" start
& "$scr\pgsql\bin\pg_isready.exe" -h 127.0.0.1 -p 55432
```

`initdb` refuses to run from an elevated (Administrator) shell — use a normal one. Stop it later with `pg_ctl.exe -D "$scr\pgdata" stop`. Nothing here is committed or required of developers.

- [ ] **Step 2: Run the lane locally**

```powershell
$env:BESTTEAM_TEST_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:55432/postgres"
.\.venv\Scripts\python.exe -m pytest -m "not e2e and not optional" -q -p no:cacheprovider 2>&1 | Tee-Object "$scr\lane.log" | Select-Object -Last 60
```

First confirm the harness itself: `tests/test_test_engine.py` and `tests/test_migrations_postgres.py` pass, and `SELECT count(*) FROM pg_database` (via `psql`-less Python: `psycopg.connect("postgresql://postgres@127.0.0.1:55432/postgres").execute("select count(*) from pg_database").fetchone()`) stays small between runs — the per-test drop works.

- [ ] **Step 3: Triage every failure by class**

Work through `lane.log`. Classes, with the grep that finds the rest of each:

| Class | Symptom | Fix |
|---|---|---|
| Foreign key (kind 1/2 of Task 12) | `ForeignKeyViolation` | as Task 12 |
| Boolean compared to an integer | `operator does not exist: boolean = integer` | `grep -rnE "== [01]\b" ui/backend tests` on boolean columns → compare with `True`/`False` or use the column itself |
| JSON column compared or `DISTINCT`ed | `could not identify an equality operator for type json` | `grep -rnE "\.(config|payload|trigger_context|context|requirements_json|specification_json) ==" ui/backend` → compare by id, or cast to text |
| SQLite error text asserted | test expects `UNIQUE constraint failed` | assert on `IntegrityError`, not its message |
| Type leniency | `invalid input syntax for type integer` etc. | the fixture inserted the wrong Python type; fix the fixture |
| Order without `ORDER BY` | a list assertion in a different order | add the ordering the endpoint promises, or compare as sets; if the *endpoint* has no ordering, that is a product finding — add one and a test |
| Shared-connection semantics | a test that only works on `:memory:` | `@pytest.mark.sqlite_only` with a reason |
| Raw `sqlite3` / `PRAGMA` in a test | `syntax error at or near "PRAGMA"` | `sqlite_only`, unless the test is about the file itself (then it is already not using `make_test_engine`) |

Product fixes get their own `fix(...)` commit with a regression test; fixture fixes are batched per file. Re-run the lane until green, then push and confirm `backend-postgres` is green on the draft PR.

- [ ] **Step 4: Record the lane's duration**

From the CI run, note the `backend-postgres` wall time. Spec §8: if it exceeds ~15 minutes, switch `_postgres.py` to one database per test *module* with `TRUNCATE ... RESTART IDENTITY CASCADE` between tests and note it in the PR. Otherwise leave per-test clones.

- [ ] **Step 5: Commit**

Already committed per fix. Final: `git push`, `gh pr ready` (mark the draft ready once green).

### Task 17: docs, full gates, PR 2 ready (spec §12, §14)

**Files:**
- Modify: `ui/backend/db/CLAUDE.md` (the foreign-key bullet), root `CLAUDE.md` ("Testing notes"), `docs/STATUS.md`

- [ ] **Step 1: `ui/backend/db/CLAUDE.md`** — replace the foreign-key bullet from Task 9 with:

```markdown
- **SQLite foreign-key enforcement is off in production** — several notes
  below depend on knowing that nothing catches a dangling FK for you there.
  The *test* engine (`tests/helpers.py::make_test_engine`) turns it on and
  Postgres always enforces, so the suite is where a child-before-parent write
  is caught: write parents first.
```

- [ ] **Step 2: root `CLAUDE.md`** — add one bullet to "Testing notes":

```markdown
- **Every fixture builds its engine with `tests/helpers.make_test_engine()`**
  (`:memory:`) or `make_test_engine(tmp_path)` (a file, for anything with two
  live Sessions). Both enforce foreign keys. With `BESTTEAM_TEST_DATABASE_URL`
  set (the `backend-postgres` CI job) each call is a fresh Postgres database
  cloned from a template; `sqlite_only` marks the few tests that depend on
  `:memory:`'s shared connection. That lane runs serially on every backend PR.
```

- [ ] **Step 3: STATUS.md** — insert at the top of `## Done`:

```markdown
- **The backend suite runs against Postgres on every backend PR, and the
  SQLite suite enforces foreign keys** (2026-09-<dd>, PR 2 of 3 for
  `specs/2026-09-07-database-engine-portability-design.md`). One test-engine
  entry point (`make_test_engine`) replaced 121 direct engine constructions;
  on the `backend-postgres` lane every call is a fresh database cloned from a
  per-session template and dropped after the test. The 41-migration chain
  replays on Postgres and lands on exactly `create_all`'s schema. Enforcing
  foreign keys in the test engine found <N> fixture defects and <M> product
  defects — the notable one: a run whose row could not be written up front
  still persisted its `run_failed` trace event with no parent (the three
  2026-09-05 orphans); the failure path now writes the row first. Lane
  duration: <T> minutes.
```

Fill `<dd>`, `<N>`, `<M>`, `<T>`.

- [ ] **Step 4: Gates and PR**

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not e2e" -n auto
.\.venv\Scripts\python.exe -m pytest tests/e2e -m "e2e and not slow"
```

```bash
git add ui/backend/db/CLAUDE.md CLAUDE.md docs/STATUS.md
git commit -m "docs: the test engine, the Postgres lane, and what enforcing foreign keys found"
git push
gh pr ready
gh pr checks --watch
```

Expected: `backend-unit-integration`, `backend-postgres`, `e2e-smoke` green.

---

## PR 3 — `admin migrate-db`

Cut `feat/db-engine-portability-migrate` from the head of PR 2.

### Task 18: the orphan report and the head check (spec §11, pre-flight items 1 and 4)

**Files:**
- Create: `ui/backend/db/migrate.py`
- Test: `tests/test_migrate_db.py`

**Interfaces:**
- Produces in `ui/backend/db/migrate.py`:
  - `class MigrateError(RuntimeError)`
  - `@dataclass(frozen=True) Orphan(table: str, column: str, parent: str, rows: int, nullable: bool)`
  - `orphan_report(engine: Engine) -> list[Orphan]`
  - `stamped_revision(engine: Engine) -> str | None`
  - `head_revision() -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrate_db.py`:

```python
"""`admin migrate-db` (spec §11): SQLite → SQLite everywhere, SQLite → Postgres
on the lane. The source is seeded through the plain `make_engine` (no
foreign-key enforcement, like the production file) so orphans can be planted."""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]  # stamps through Alembic
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

from alembic import command
from alembic.config import Config

import _postgres
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.database import sqlite_url_for
from ui.backend.db.inbox_events import record_events
from ui.backend.db.migrate import MigrateError, head_revision, orphan_report, stamped_revision
from ui.backend.db.models import Organization, PipelineRecord, Run, TraceEventRecord, UsageRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.users import create_user

_ROOT = Path(__file__).resolve().parent.parent


def _stamp_head(url: str) -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.stamp(cfg, "head")


def _seed_source(path: Path) -> str:
    """Every kind of row the copy has to get right, plus the two orphan kinds
    the development database showed on 2026-09-07."""
    url = sqlite_url_for(path)
    engine = make_engine(url)
    init_db(engine)
    with session_factory(engine)() as db:
        org = get_or_create_org(db, "acme", "Acme")
        create_user(db, "alice", "pw", org_id=org.id)
        db.add(PipelineRecord(name="team", org_id=org.id, config={"agents": [{"name": "a"}]}, status="deployed"))
        db.add(Run(id="run-1", pipeline="team", input="hello", org_id=org.id, username="alice", status="completed"))
        db.add(Run(id="run-2", pipeline="team", input="again", org_id=org.id, username="alice",
                   status="failed", retry_of_run_id="run-1"))
        db.add(TraceEventRecord(run_id="run-1", seq=0, type="run_queued"))
        db.add(TraceEventRecord(run_id="run-2", seq=0, type="run_failed", data='"x"'))
        db.add(UsageRecord(run_id="run-1", org_id=org.id, agent="a", model="fake:x", input_tokens=1, output_tokens=2))
        db.add(UsageRecord(run_id="ghost", org_id=org.id, agent="a", model="fake:x"))        # nullable FK dangles
        db.add(TraceEventRecord(run_id="ghost-2", seq=0, type="run_failed"))                  # NOT NULL FK dangles
        record_events(db, org_id=org.id, mailbox_identity="m", mailbox_generation="g", external_ids=["1", "2"])
        db.commit()
    engine.dispose()
    _stamp_head(url)
    return url


def test_orphan_report_names_both_kinds(tmp_path):
    url = _seed_source(tmp_path / "src.db")
    engine = make_engine(url)
    try:
        found = {(o.table, o.column, o.parent, o.rows, o.nullable) for o in orphan_report(engine)}
        assert stamped_revision(engine) == head_revision()
    finally:
        engine.dispose()
    assert found == {
        ("usage_records", "run_id", "runs", 1, True),
        ("trace_events", "run_id", "runs", 1, False),
    }


def test_an_unstamped_source_has_no_revision(tmp_path):
    engine = make_engine(tmp_path / "plain.db")
    init_db(engine)
    try:
        assert stamped_revision(engine) is None
    finally:
        engine.dispose()
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrate_db.py -v` → FAIL, `No module named 'ui.backend.db.migrate'`.

- [ ] **Step 2: Create `ui/backend/db/migrate.py`**

```python
"""`admin migrate-db`: copy this deployment's database into an EMPTY one.

Spec: docs/superpowers/specs/2026-09-07-database-engine-portability-design.md
§11. Engine-agnostic on purpose -- SQLite → Postgres is the case it exists
for, SQLite → SQLite is how it is tested everywhere. The source is opened
read-only and never written.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy import Engine, Table, inspect, select, text

from .database import describe_database_url, make_engine, readonly_engine
from .models import Base

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "alembic"
_INI = _REPO_ROOT / "alembic.ini"


class MigrateError(RuntimeError):
    """A refusal. Unless the message says otherwise, nothing was written."""


@dataclasses.dataclass(frozen=True)
class Orphan:
    table: str
    column: str
    parent: str
    rows: int
    nullable: bool


def _pk_of(table: Table, record: dict):
    columns = list(table.primary_key.columns)
    return record[columns[0].name] if len(columns) == 1 else tuple(record[c.name] for c in columns)


def orphan_report(engine: Engine) -> List[Orphan]:
    """Every foreign key in the schema with child rows whose parent is missing."""
    out: List[Orphan] = []
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            for fk in table.foreign_keys:
                child, parent = fk.parent, fk.column
                dangling = (
                    select(sa.func.count())
                    .select_from(table)
                    .where(child.isnot(None))
                    .where(~sa.exists().where(parent == child))
                )
                rows = conn.execute(dangling).scalar_one()
                if rows:
                    out.append(Orphan(table.name, child.name, parent.table.name, rows, bool(child.nullable)))
    return out


def stamped_revision(engine: Engine) -> Optional[str]:
    with engine.connect() as conn:
        if not inspect(conn).has_table("alembic_version"):
            return None
        row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
    return row[0] if row else None


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory(str(_SCRIPT_LOCATION)).get_current_head()
```

- [ ] **Step 3: Run the tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrate_db.py -v` → both PASS.

- [ ] **Step 4: Commit**

```bash
git add ui/backend/db/migrate.py tests/test_migrate_db.py
git commit -m "feat(db): orphan report and stamp check for migrate-db"
```

### Task 19: pre-flight, copy, sequence reset, verification, `run_migration` (spec §11)

**Files:**
- Modify: `ui/backend/db/migrate.py`
- Test: `tests/test_migrate_db.py`

**Interfaces:**
- Produces:
  - `@dataclass TableCopy(copied: int = 0, skipped: int = 0, nulled: int = 0, skipped_keys: list[str])`
  - `@dataclass CopyReport(tables: dict[str, TableCopy])`
  - `preflight(source: Engine, target: Engine, *, source_url: str, target_url: str, fix_orphans: bool) -> list[Orphan]`
  - `upgrade_target(target_url: str) -> None`
  - `copy_rows(source: Engine, target: Engine, *, fix_orphans: bool, batch_size: int, log: Callable[[str], None]) -> CopyReport`
  - `reset_sequences(target: Engine) -> None`
  - `verify_copy(source: Engine, target: Engine, report: CopyReport) -> list[str]`
  - `run_migration(source_url: str, target_url: str, *, fix_orphans: bool = False, batch_size: int = 1000, log: Callable[[str], None] = print) -> int` (0 ok, 1 verification failed; raises `MigrateError` on a refusal)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrate_db.py`:

```python
from ui.backend.db.migrate import run_migration


def _count(url: str, model) -> int:
    engine = make_engine(url)
    try:
        with session_factory(engine)() as db:
            return db.query(model).count()
    finally:
        engine.dispose()


def test_refuses_orphans_without_the_flag_and_writes_no_rows(tmp_path):
    src = _seed_source(tmp_path / "src.db")
    target = sqlite_url_for(tmp_path / "dst.db")
    with pytest.raises(MigrateError, match="fix-orphans"):
        run_migration(src, target, log=lambda _line: None)
    assert not (tmp_path / "dst.db").exists() or _count(target, Organization) == 0


def test_copies_everything_with_orphans_fixed_and_the_source_untouched(tmp_path):
    src_path = tmp_path / "src.db"
    src = _seed_source(src_path)
    before = src_path.read_bytes()
    target = sqlite_url_for(tmp_path / "dst.db")
    lines = []

    assert run_migration(src, target, fix_orphans=True, log=lines.append) == 0

    assert src_path.read_bytes() == before
    assert _count(target, Organization) == 1
    assert _count(target, Run) == 2
    assert _count(target, TraceEventRecord) == 2   # ghost-2 skipped (NOT NULL key)
    assert _count(target, UsageRecord) == 2        # ghost kept, run_id set to NULL
    engine = make_engine(target)
    try:
        with session_factory(engine)() as db:
            assert db.get(Run, "run-2").retry_of_run_id == "run-1"
            assert db.query(UsageRecord).filter_by(run_id=None).count() == 1
            assert db.get(Organization, 1).name == "acme"  # primary keys preserved
    finally:
        engine.dispose()
    assert any("verified" in line for line in lines)
    assert any("trace_events" in line and "1 skipped" in line for line in lines)


def test_refuses_a_non_empty_target_and_the_same_url(tmp_path):
    src = _seed_source(tmp_path / "src.db")
    other = _seed_source(tmp_path / "other.db")
    with pytest.raises(MigrateError, match="already holds rows"):
        run_migration(src, other, fix_orphans=True, log=lambda _line: None)
    with pytest.raises(MigrateError, match="same database"):
        run_migration(src, src, fix_orphans=True, log=lambda _line: None)


def test_refuses_a_source_behind_head(tmp_path):
    src_path = tmp_path / "old.db"
    engine = make_engine(src_path)
    init_db(engine)
    engine.dispose()  # tables, but no Alembic stamp
    with pytest.raises(MigrateError, match="alembic upgrade head"):
        run_migration(sqlite_url_for(src_path), sqlite_url_for(tmp_path / "dst.db"), log=lambda _line: None)
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrate_db.py -v` → the four new tests FAIL on import (`run_migration`).

- [ ] **Step 2: Implement the rest of `migrate.py`**

Append to `ui/backend/db/migrate.py`:

```python
@dataclasses.dataclass
class TableCopy:
    copied: int = 0
    skipped: int = 0   # rows dropped because a NOT NULL foreign key dangled
    nulled: int = 0    # nullable foreign keys written as NULL
    skipped_keys: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class CopyReport:
    tables: Dict[str, TableCopy] = dataclasses.field(default_factory=dict)


def preflight(source: Engine, target: Engine, *, source_url: str, target_url: str, fix_orphans: bool) -> List[Orphan]:
    """Refuse before a single row moves (spec §11, pre-flight 1-4)."""
    if source_url == target_url:
        raise MigrateError("source and target are the same database")
    stamped, head = stamped_revision(source), head_revision()
    if stamped != head:
        raise MigrateError(f"the source is stamped {stamped}, head is {head}; run `alembic upgrade head` on it first")
    existing = set(inspect(target).get_table_names())
    with target.connect() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name in existing and conn.execute(select(sa.func.count()).select_from(table)).scalar_one():
                raise MigrateError(f"the target already holds rows in {table.name}; migrate-db fills an EMPTY database only")
    orphans = orphan_report(source)
    if orphans and not fix_orphans:
        detail = ", ".join(f"{o.table}.{o.column} -> {o.parent}: {o.rows}" for o in orphans)
        raise MigrateError(
            f"the source has rows whose foreign key points at nothing ({detail}). Rerun with "
            "--fix-orphans to write the nullable ones as NULL and skip the rest, or clean them up first"
        )
    return orphans


def upgrade_target(target_url: str) -> None:
    """`alembic upgrade head` on the target: the same path the Docker entrypoint takes (Ruling 6)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_INI))
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", target_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def copy_rows(source: Engine, target: Engine, *, fix_orphans: bool, batch_size: int,
              log: Callable[[str], None]) -> CopyReport:
    """Copy every model table in foreign-key order, one transaction on the target.

    Core `select`/`insert` on the model tables, so JSON, boolean and datetime
    values go through the column types on both sides. A self-referencing
    foreign key (`runs.retry_of_run_id`, `runs.diagnostic_of_run_id`) is
    written as NULL first and patched after the table is in, so row order
    never matters. Orphan policy (Ruling 8) applies to the copy stream only.
    """
    report = CopyReport()
    seen: Dict[str, set] = {}
    with source.connect() as src, target.begin() as dst:
        for table in Base.metadata.sorted_tables:
            stats = TableCopy()
            pk_columns = list(table.primary_key.columns)
            self_fks = [fk for fk in table.foreign_keys if fk.column.table is table]
            if self_fks and len(pk_columns) != 1:
                raise MigrateError(f"{table.name}: a self-referencing key on a composite primary key is not supported")
            all_keys = {row[0] for row in src.execute(select(pk_columns[0]))} if self_fks else set()
            keys: set = set()
            patches: List[dict] = []
            batch: List[dict] = []
            for row in src.execute(select(table).order_by(*pk_columns)).mappings():
                record = dict(row)
                drop = False
                for fk in table.foreign_keys:
                    value = record[fk.parent.name]
                    if value is None:
                        continue
                    is_self = fk.column.table is table
                    parents = all_keys if is_self else seen.get(fk.column.table.name, set())
                    if value in parents:
                        if is_self:
                            patches.append({"pk": _pk_of(table, record), "column": fk.parent.name, "value": value})
                            record[fk.parent.name] = None
                        continue
                    if not fix_orphans:
                        raise MigrateError(f"{table.name}.{fk.parent.name}={value!r} points at a missing "
                                           f"{fk.column.table.name} row (pre-flight should have refused)")
                    if fk.parent.nullable:
                        record[fk.parent.name] = None
                        stats.nulled += 1
                    else:
                        drop = True
                        stats.skipped_keys.append(str(_pk_of(table, record)))
                        break
                if drop:
                    stats.skipped += 1
                    continue
                keys.add(_pk_of(table, record))
                batch.append(record)
                if len(batch) >= batch_size:
                    dst.execute(table.insert(), batch)
                    stats.copied += len(batch)
                    batch = []
            if batch:
                dst.execute(table.insert(), batch)
                stats.copied += len(batch)
            for column_name in sorted({p["column"] for p in patches}):
                rows = [{"b_pk": p["pk"], "b_value": p["value"]} for p in patches
                        if p["column"] == column_name and p["value"] in keys]
                if rows:
                    dst.execute(
                        table.update()
                        .where(pk_columns[0] == sa.bindparam("b_pk"))
                        .values({column_name: sa.bindparam("b_value")}),
                        rows,
                    )
                stats.nulled += sum(1 for p in patches if p["column"] == column_name and p["value"] not in keys)
            seen[table.name] = keys
            report.tables[table.name] = stats
            extra = f", {stats.skipped} skipped, {stats.nulled} foreign keys nulled" if stats.skipped or stats.nulled else ""
            log(f"  {table.name}: {stats.copied} copied{extra}")
    return report


def reset_sequences(target: Engine) -> None:
    """Postgres: after inserting explicit ids, move each serial past max(id)."""
    if target.dialect.name != "postgresql":
        return
    with target.begin() as conn:
        for table in Base.metadata.sorted_tables:
            columns = list(table.primary_key.columns)
            if len(columns) != 1 or not isinstance(columns[0].type, sa.Integer):
                continue
            conn.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence(:t, :c), "
                    f'COALESCE((SELECT MAX("{columns[0].name}") FROM "{table.name}"), 0) + 1, false)'
                ),
                {"t": table.name, "c": columns[0].name},
            )


def verify_copy(source: Engine, target: Engine, report: CopyReport) -> List[str]:
    """Row counts and primary keys per table; the list is empty when they match."""
    problems: List[str] = []
    with source.connect() as src, target.connect() as dst:
        for table in Base.metadata.sorted_tables:
            stats = report.tables[table.name]
            src_n = src.execute(select(sa.func.count()).select_from(table)).scalar_one()
            dst_n = dst.execute(select(sa.func.count()).select_from(table)).scalar_one()
            if dst_n != src_n - stats.skipped:
                problems.append(f"{table.name}: source {src_n} minus {stats.skipped} skipped != target {dst_n}")
                continue
            pk = list(table.primary_key.columns)

            def _keys(conn):
                return sorted(str(r[0] if len(pk) == 1 else tuple(r)) for r in conn.execute(select(*pk)))

            skipped = set(stats.skipped_keys)
            if [k for k in _keys(src) if k not in skipped] != _keys(dst):
                problems.append(f"{table.name}: primary keys differ")
    return problems


def run_migration(source_url: str, target_url: str, *, fix_orphans: bool = False,
                  batch_size: int = 1000, log: Callable[[str], None] = print) -> int:
    log(f"source: {describe_database_url(source_url)}")
    log(f"target: {describe_database_url(target_url)}")
    source = readonly_engine(source_url)
    target = make_engine(target_url)
    try:
        orphans = preflight(source, target, source_url=source_url, target_url=target_url, fix_orphans=fix_orphans)
        for orphan in orphans:
            action = "written as NULL" if orphan.nullable else "skipped"
            log(f"  orphans: {orphan.table}.{orphan.column} -> {orphan.parent}: {orphan.rows} row(s), will be {action}")
        log("alembic upgrade head on the target")
        upgrade_target(target_url)
        log("copying rows")
        report = copy_rows(source, target, fix_orphans=fix_orphans, batch_size=batch_size, log=log)
        reset_sequences(target)
        problems = verify_copy(source, target, report)
        for problem in problems:
            log(f"[FAIL] {problem}")
        if problems:
            log("the target is partially populated; drop it and rerun")
            return 1
        log("verified: every table's row count and primary keys match the source")
        log("NOT copied: the per-user memory store (BESTTEAM_MEMORY_DB) and the files under "
            "ui/backend/data (uploads, knowledge-base versions) -- they stay where they are. "
            "To switch the backend over, set BESTTEAM_DATABASE_URL to the target and restart.")
        return 0
    finally:
        source.dispose()
        target.dispose()
```

- [ ] **Step 3: Run the tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrate_db.py -v`
Expected: all six PASS. If `test_copies_everything...` fails on `db.get(Organization, 1)`, check that `copy_rows` inserts explicit ids (Core `insert` with the id present does) — do not "fix" by dropping the assertion.

- [ ] **Step 4: Commit**

```bash
git add ui/backend/db/migrate.py tests/test_migrate_db.py
git commit -m "feat(db): migrate-db copies a database into an empty one with pre-flight, orphan policy and verification"
```

### Task 20: the `migrate-db` subcommand (spec §11)

**Files:**
- Modify: `ui/backend/admin.py:1-25` (docstring example), `:185-200` (parser), `:201` (handler before `check-env`)
- Test: `tests/test_migrate_db.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_migrate_db.py`:

```python
def test_cli_exit_codes_and_messages(tmp_path, monkeypatch, capsys):
    from ui.backend import admin

    src_path = tmp_path / "src.db"
    _seed_source(src_path)
    monkeypatch.setenv("BESTTEAM_DB_PATH", str(src_path))
    monkeypatch.delenv("BESTTEAM_DATABASE_URL", raising=False)
    target = sqlite_url_for(tmp_path / "dst.db")

    assert admin.main(["migrate-db", "--to", target]) == 1
    assert "[FAIL] migrate-db" in capsys.readouterr().out

    assert admin.main(["migrate-db", "--to", target, "--fix-orphans"]) == 0
    out = capsys.readouterr().out
    assert "verified" in out
    assert "NOT copied" in out
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrate_db.py -v -k cli` → FAIL (`argument command: invalid choice: 'migrate-db'`).

- [ ] **Step 2: Wire the subcommand**

In `admin.py`'s module docstring add the example line
`    docker compose run --rm --no-deps backend python -m ui.backend.admin migrate-db --to postgresql+psycopg://user:pw@host/bestteam --fix-orphans`.

After the `check-health` `sub.add_parser(...)` block:

```python
    migrate_p = sub.add_parser(
        "migrate-db",
        help="copy this deployment's database into an EMPTY database at --to "
             "(e.g. postgresql+psycopg://user:pw@host/bestteam): pre-flight checks, "
             "orphan policy, sequence reset and verification. Never writes to the source.",
    )
    migrate_p.add_argument("--to", dest="target_url", required=True,
                           help="SQLAlchemy URL of the empty target database")
    migrate_p.add_argument("--fix-orphans", action="store_true",
                           help="write a nullable dangling foreign key as NULL and skip a row "
                                "whose NOT NULL one dangles (both reported); refused otherwise")
    migrate_p.add_argument("--batch-size", type=int, default=1000)
```

Before `if args.command == "check-env":`:

```python
    if args.command == "migrate-db":
        from .db.migrate import MigrateError, run_migration

        try:
            return run_migration(
                default_database_url(os.environ),
                args.target_url,
                fix_orphans=args.fix_orphans,
                batch_size=args.batch_size,
                log=print,
            )
        except MigrateError as exc:
            print(f"[FAIL] migrate-db: {exc}")
            return 1
```

- [ ] **Step 3: Run and commit**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_migrate_db.py tests/test_admin_cli.py -v` → PASS.

```bash
git add ui/backend/admin.py tests/test_migrate_db.py
git commit -m "feat(admin): migrate-db subcommand"
```

### Task 21: SQLite → Postgres on the lane, and a local rehearsal (spec §11, §14)

**Files:**
- Test: `tests/test_migrate_db.py`

- [ ] **Step 1: Write the lane-only test**

Append to `tests/test_migrate_db.py`:

```python
@pytest.mark.skipif(not _postgres.enabled(), reason="BESTTEAM_TEST_DATABASE_URL is not set")
def test_copies_into_postgres_and_resets_the_sequences(tmp_path):
    src = _seed_source(tmp_path / "src.db")
    target = _postgres.empty_database_url().render_as_string(hide_password=False)

    assert run_migration(src, target, fix_orphans=True, log=lambda _line: None) == 0

    engine = make_engine(target)
    try:
        with session_factory(engine)() as db:
            assert db.query(Run).count() == 2
            assert db.get(Run, "run-2").retry_of_run_id == "run-1"
            db.add(Organization(name="next"))
            db.commit()  # would collide on id=1 without the sequence reset
            assert db.query(Organization).count() == 2
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run it against the portable server from Task 16**

```powershell
$env:BESTTEAM_TEST_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:55432/postgres"
.\.venv\Scripts\python.exe -m pytest tests/test_migrate_db.py -v
```

Expected: all PASS, including the Postgres one. Then push and confirm `backend-postgres` on the PR runs it (it is `slow`, and the lane's `-m` does not exclude `slow`).

- [ ] **Step 3: Rehearse with the development database**

```powershell
Copy-Item ui\backend\data\bestteam.db "$scr\rehearsal-src.db"
.\.venv\Scripts\python.exe -c "import psycopg; psycopg.connect('postgresql://postgres@127.0.0.1:55432/postgres', autocommit=True).execute('CREATE DATABASE rehearsal')"
$env:BESTTEAM_DB_PATH = "$scr\rehearsal-src.db"
.\.venv\Scripts\python.exe -m ui.backend.admin migrate-db --to postgresql+psycopg://postgres@127.0.0.1:55432/rehearsal
```

Expected: refused, naming `usage_records.run_id -> runs: 101` and `trace_events.run_id -> runs: 3` (the 2026-09-07 finding; the counts may have moved since). Then rerun with `--fix-orphans`; expected exit 0 and `verified`. Then boot the backend against it and log in with the dev account:

```powershell
$env:BESTTEAM_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:55432/rehearsal"
.\.venv\Scripts\python.exe -m uvicorn ui.backend.main:app --port 8000 --host 127.0.0.1
```

`curl -s http://127.0.0.1:8000/api/health` → 200; log in from the frontend; open the Activity page and one run's trace. Paste the `migrate-db` output (both runs) into the PR description. Stop the server and unset both variables.

- [ ] **Step 4: Commit**

```bash
git add tests/test_migrate_db.py
git commit -m "test: migrate-db into Postgres resets the sequences"
```

### Task 22: docs, gates, PR 3 (spec §12, §14)

**Files:**
- Modify: `docs/ADMIN_GUIDE.md` (CLI list), `docs/deployment.md` §3, `docs/STATUS.md`

- [ ] **Step 1: Docs**

`docs/ADMIN_GUIDE.md`: in the §3 "Command reference" table, add a row after `check-health`:

```markdown
| `migrate-db --to <url> [--fix-orphans]` | Copy this deployment's database into an **empty** one — the Postgres cutover's first step. Pre-flight refuses a source behind head, a non-empty target, the same URL, and dangling foreign keys unless `--fix-orphans` (nullable → NULL, NOT NULL → row skipped, both reported). Never writes to the source. The memory store and the files under `ui/backend/data` are not copied. |
```

`docs/deployment.md` §3, at the end: `` Moving to a server database is not a migration but a copy: `admin migrate-db --to <url>` (see the ADMIN_GUIDE). The procedure around it — provisioning, backups, the cutover window — is the ops half of the 2026-09-07 spec and is not written yet. ``

`docs/STATUS.md`, top of `## Done`:

```markdown
- **`admin migrate-db` copies the deployment database into an empty one**
  (2026-09-<dd>, PR 3 of 3 for
  `specs/2026-09-07-database-engine-portability-design.md`). Pre-flight
  refuses a source behind head, a non-empty target, the same URL, and
  dangling foreign keys unless `--fix-orphans` (nullable → NULL, NOT NULL →
  row skipped, both reported with keys); the target's schema comes from
  `alembic upgrade head`; rows go through the model tables' types in
  dependency order with self-references patched afterwards; Postgres
  sequences are reset; counts and primary keys are verified. Rehearsed on a
  copy of the development database into a local Postgres: <X> rows across
  <Y> tables, <Z> orphans handled. The code half of the spec is complete;
  the ops half (provisioning, backup/restore, runbook, cutover) waits for
  the trigger in `DECISIONS.md`.
```

- [ ] **Step 2: Gates and PR**

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not e2e" -n auto
```

```bash
git add docs/ADMIN_GUIDE.md docs/deployment.md docs/STATUS.md
git commit -m "docs: migrate-db and the end of the code half"
git push -u origin feat/db-engine-portability-migrate
gh pr create --base main --title "Database engine portability (3/3): admin migrate-db" --body-file <scratchpad>/pr3.md
gh pr checks --watch
```

`pr3.md` carries the rehearsal output from Task 21 and the trailer.

---

## Self-review notes (kept for the executor)

- Spec §3 → Tasks 1–4; §4 → Tasks 5–6; §5 → Tasks 11–13; §6 → Task 7; §7 → Task 8; §8 → Tasks 11, 12, 14; §9 → Task 15; §10 → Task 16; §11 → Tasks 18–21; §12 → Tasks 9, 10, 17, 22; §13 is implemented across Tasks 2, 7 and 19; §14 is the PR table above; §15's measurements land in Task 10 (SQL per request) and Task 16 (lane duration).
- Names used across tasks: `resolve_database_url`, `sqlite_url_for`, `sqlite_path_of`, `describe_database_url`, `lock_anchor_for`, `readonly_engine`, `MEMORY_URL`, `DATA_DIR` (Task 1) — consumed by Tasks 3, 4, 7, 18–19. `make_test_engine` (Task 11) — consumed by Tasks 12, 13. `_postgres.enabled / clone_engine / empty_database_url / drop_created / drop_template / url_for` (Task 11) — consumed by Tasks 14, 21 and conftest. `default_database_url` (Task 7) — consumed by Task 20. `run_migration` signature (Task 19) — consumed by Tasks 20, 21.
- Placeholders that are deliberate and must be filled at execution time, never left: `<N1>`/`<N2>`/`<N3>` (Task 10), `<N>`/`<M>`/`<T>`/`<dd>` (Task 17), `<X>`/`<Y>`/`<Z>`/`<dd>` (Task 22), `<scratchpad>` (the session's scratchpad path).
