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
    """Every foreign key in the schema with child rows whose parent is missing.

    `inbox_events.run_id` and `email_triggers.last_run_id` are loose pointers,
    deliberately not foreign keys (migration `z3a4b5c6d7e8`), so they are
    never scanned here.
    """
    out: List[Orphan] = []
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            for fk in table.foreign_keys:
                child, parent = fk.parent, fk.column
                # Alias the parent table: two keys here are self-referential
                # (runs.retry_of_run_id / runs.diagnostic_of_run_id -> runs.id).
                # Without the alias SQLAlchemy correlates every reference to
                # `table` to the same outer row, so the EXISTS compiles as
                # `runs.id = runs.retry_of_run_id` -- a self-comparison that is
                # never true -- and every retry run would be reported as an
                # orphan. The alias forces a genuinely correlated subquery.
                parent_table = parent.table.alias("parent")
                dangling = (
                    select(sa.func.count())
                    .select_from(table)
                    .where(child.isnot(None))
                    .where(~sa.exists().where(parent_table.c[parent.name] == child))
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
