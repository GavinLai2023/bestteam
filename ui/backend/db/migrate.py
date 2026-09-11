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
from sqlalchemy.engine import make_url
from sqlalchemy.exc import CircularDependencyError
from sqlalchemy.schema import sort_tables_and_constraints

from .database import describe_database_url, make_engine, readonly_engine, sqlite_path_of
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

    `inbox_events.run_id`, `email_triggers.last_run_id` (migration
    `z3a4b5c6d7e8`) and `usage_records.ingestion_job_id` (`c6d7e8f9g0h1`) are
    loose pointers, deliberately not foreign keys, so they are never scanned
    here.
    """
    out: List[Orphan] = []
    with engine.connect() as conn:
        for table in Base.metadata.tables.values():
            for fk in table.foreign_keys:
                child, parent = fk.parent, fk.column
                # Alias the parent table: two keys here are self-referential
                # (runs.retry_of_run_id / runs.diagnostic_of_run_id -> runs.id).
                # Without the alias the subquery carries its own `FROM runs`,
                # and plain SQL name scoping makes BOTH sides of
                # `runs.id = runs.retry_of_run_id` refer to that inner table --
                # an uncorrelated, table-global existence check ("does any run
                # retry itself?"), false on ordinary data, so every retry run
                # would be reported as an orphan. The alias leaves the child
                # column bound to the outer row, so the subquery is genuinely
                # correlated.
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
        for table in Base.metadata.tables.values():
            if table.name in existing and conn.execute(select(sa.func.count()).select_from(table)).scalar_one():
                raise MigrateError(
                    f"the target already holds rows in {table.name}; migrate-db fills an EMPTY "
                    "database only; drop and recreate the target (migrate-db runs the migration "
                    "chain itself)"
                )
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


def _copy_order() -> List[Table]:
    """The model tables in an order every NOT NULL foreign key can be satisfied.

    `Base.metadata.sorted_tables` cannot order this schema: `pipelines` and
    `pipeline_versions` (and `skills` / `skill_versions`) reference each other
    through the nullable `current_version_id` head pointers, so it warns and
    breaks the cycles arbitrarily. Breaking them at nullable keys only keeps
    every NOT NULL key pointing at a table copied earlier; the nullable keys
    that still point forward are written as NULL and patched once their table
    is in (see `copy_rows`).
    """

    def breakable(constraint: sa.ForeignKeyConstraint):
        # None: a dependency that may be dropped inside a cycle. False: never dropped.
        return None if all(column.nullable for column in constraint.columns) else False

    try:
        pairs = sort_tables_and_constraints(list(Base.metadata.tables.values()), filter_fn=breakable)
    except CircularDependencyError as exc:
        raise MigrateError(
            f"the schema has a cycle of NOT NULL foreign keys, so the copy cannot be ordered: {exc}"
        ) from exc
    return [table for table, _constraints in pairs if table is not None]


def _forward_pointing_fks(table: Table, position: Dict[str, int]) -> List[sa.ForeignKey]:
    """`table`'s own foreign keys whose parent sits at or after it in copy order.

    These are the head-pointer and self-reference cycles `_copy_order` breaks:
    `copy_rows` writes them as NULL and patches them once every table is
    loaded; `clear_migration_seed` must null them out first so an engine that
    enforces foreign keys accepts the reverse-copy-order deletes.
    """
    return [fk for fk in table.foreign_keys if position[fk.column.table.name] >= position[table.name]]


def clear_migration_seed(target: Engine, log: Callable[[str], None]) -> None:
    """Delete whatever the migration chain seeded, so the copy starts empty.

    Pre-flight proved the target held no rows; `alembic upgrade head` then ran,
    and migration `b7c8d9e0f1a2` seeds the `default` organisation. That row is
    schema bootstrap, not data -- and the source carries a `default` org of its
    own, which would collide on both the primary key and the unique name. Safe
    against any populated head-pointer cycle the chain might one day seed, not
    just this one row: every forward-pointing foreign key
    (`_forward_pointing_fks`, the same test `copy_rows` applies) is nulled out
    first, table by table in copy order, then each table is deleted in reverse
    copy order -- the same null-then-delete shape `copy_rows` uses to write
    rows, so an engine that enforces foreign keys accepts both phases.
    """
    order = _copy_order()
    position = {table.name: index for index, table in enumerate(order)}
    cleared: Dict[str, int] = {}
    with target.begin() as conn:
        for table in order:
            forward = _forward_pointing_fks(table, position)
            if forward:
                conn.execute(table.update().values({fk.parent.name: None for fk in forward}))
        for table in reversed(order):
            deleted = conn.execute(table.delete()).rowcount
            if deleted:
                cleared[table.name] = deleted
    if cleared:
        detail = ", ".join(f"{name}: {rows}" for name, rows in sorted(cleared.items()))
        log(f"  cleared the rows the migration chain seeds ({detail})")


def copy_rows(source: Engine, target: Engine, *, fix_orphans: bool, batch_size: int,
              log: Callable[[str], None]) -> CopyReport:
    """Copy every model table in foreign-key order, one transaction on the target.

    Core `select`/`insert` on the model tables, so JSON, boolean and datetime
    values go through the column types on both sides. A foreign key that
    points forward in the copy order -- the self-referencing run pointers and
    the two `current_version_id` head pointers -- is written as NULL first and
    patched after every table is in, so row order never matters. Orphan
    policy (Ruling 8) applies to the copy stream only.

    One asymmetry has to be undone by hand: `sa.JSON` is declared with the
    default `none_as_null=False`, so its result processor maps BOTH a SQL
    NULL and a stored JSON `null` to Python `None`, and its bind processor
    then serialises `None` back as the JSON document `null`. Left alone, every
    SQL NULL in a JSON column would arrive as JSON `null` -- a difference
    `verify_copy` cannot see, and one that makes a later
    `WHERE trigger_context IS NULL` return nothing. Substituting `sa.null()`
    (`json_columns` below) makes SQL NULL round-trip exactly, at the cost of
    collapsing a stored JSON `null` to SQL NULL. That is the right trade: the
    JSON-`null`/`None` distinction is unreachable from every read path in this
    codebase, whereas SQL-level NULLness is directly observable (`IS NULL`).
    """
    report = CopyReport()
    order = _copy_order()
    position = {table.name: index for index, table in enumerate(order)}
    seen: Dict[str, set] = {}
    patches: List[dict] = []  # {"table", "pk", "column", "value", "parent"} for every forward pointer
    with source.connect() as src, target.begin() as dst:
        for table in order:
            stats = TableCopy()
            pk_columns = list(table.primary_key.columns)
            json_columns = [c.name for c in table.columns if isinstance(c.type, sa.JSON)]
            forward = set(_forward_pointing_fks(table, position))
            keys: set = set()
            batch: List[dict] = []
            for row in src.execute(select(table).order_by(*pk_columns)).mappings():
                record = dict(row)
                row_patches: List[dict] = []
                drop = False
                for fk in table.foreign_keys:
                    value = record[fk.parent.name]
                    if value is None:
                        continue
                    parent_name = fk.column.table.name
                    if fk in forward:
                        # Forward (or self) reference: resolved after every table is in.
                        if not fk.parent.nullable:
                            raise MigrateError(
                                f"{table.name}.{fk.parent.name} is a NOT NULL key pointing forward in the copy order"
                            )
                        if len(pk_columns) != 1:
                            raise MigrateError(f"{table.name}: a forward key on a composite primary key is not supported")
                        row_patches.append({"table": table, "pk": record[pk_columns[0].name],
                                            "column": fk.parent.name, "value": value, "parent": parent_name})
                        record[fk.parent.name] = None
                        continue
                    if value in seen.get(parent_name, set()):
                        continue
                    if not fix_orphans:
                        raise MigrateError(f"{table.name}.{fk.parent.name}={value!r} points at a missing "
                                           f"{parent_name} row (pre-flight should have refused)")
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
                patches.extend(row_patches)
                keys.add(_pk_of(table, record))
                for name in json_columns:
                    # A SQL NULL read back as None; `sa.null()` writes it as one
                    # again instead of the JSON document `null` (see the docstring).
                    if record[name] is None:
                        record[name] = sa.null()
                batch.append(record)
                if len(batch) >= batch_size:
                    dst.execute(table.insert(), batch)
                    stats.copied += len(batch)
                    batch = []
            if batch:
                dst.execute(table.insert(), batch)
                stats.copied += len(batch)
            seen[table.name] = keys
            report.tables[table.name] = stats
        # Every parent table is in: patch the forward pointers whose parent exists,
        # and count the rest as nulled (a source orphan, or a parent skipped above).
        grouped: Dict[tuple, List[dict]] = {}
        for patch in patches:
            grouped.setdefault((patch["table"].name, patch["column"]), []).append(patch)
        for (table_name, column_name), group in grouped.items():
            table = Base.metadata.tables[table_name]
            pk_column = list(table.primary_key.columns)[0]
            resolved = [{"b_pk": p["pk"], "b_value": p["value"]} for p in group if p["value"] in seen[p["parent"]]]
            unresolved = len(group) - len(resolved)
            if unresolved and not fix_orphans:
                raise MigrateError(f"{table_name}.{column_name} points at a missing row (pre-flight should have refused)")
            if resolved:
                dst.execute(
                    table.update()
                    .where(pk_column == sa.bindparam("b_pk"))
                    .values({column_name: sa.bindparam("b_value")}),
                    resolved,
                )
            report.tables[table_name].nulled += unresolved
    for table in order:
        stats = report.tables[table.name]
        extra = f", {stats.skipped} skipped, {stats.nulled} foreign keys nulled" if stats.skipped or stats.nulled else ""
        log(f"  {table.name}: {stats.copied} copied{extra}")
        if stats.skipped_keys:
            log(f"    skipped {table.name} rows (NOT NULL foreign key dangling): {', '.join(stats.skipped_keys)}")
    return report


def reset_sequences(target: Engine) -> None:
    """Postgres: after inserting explicit ids, move each serial past max(id)."""
    if target.dialect.name != "postgresql":
        return
    with target.begin() as conn:
        for table in Base.metadata.tables.values():
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
        for table in Base.metadata.tables.values():
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
    if batch_size < 1:
        raise MigrateError(f"--batch-size must be at least 1, not {batch_size}")
    for label, url in (("source", source_url), ("target", target_url)):
        if make_url(url).get_backend_name() == "sqlite" and sqlite_path_of(url) is None:
            raise MigrateError(f"the {label} must be a file or a server database, not an in-memory SQLite")
    # A SQLite source is opened `mode=ro`, which cannot create the file, so an
    # absent one would surface as a two-line driver error naming neither side
    # (`env_check._absent_file` answers the same question for the checklist).
    source_path = sqlite_path_of(source_url)
    if source_path is not None and not source_path.exists():
        raise MigrateError(f"the source file {source_path} does not exist")
    log(f"source: {describe_database_url(source_url)}")
    log(f"target: {describe_database_url(target_url)}")
    source = readonly_engine(source_url)
    target = None
    try:
        target = make_engine(target_url)
        orphans = preflight(source, target, source_url=source_url, target_url=target_url, fix_orphans=fix_orphans)
        for orphan in orphans:
            action = "written as NULL" if orphan.nullable else "skipped"
            log(f"  orphans: {orphan.table}.{orphan.column} -> {orphan.parent}: {orphan.rows} row(s), will be {action}")
        log("alembic upgrade head on the target")
        upgrade_target(target_url)
        clear_migration_seed(target, log)
        log("copying rows")
        report = copy_rows(source, target, fix_orphans=fix_orphans, batch_size=batch_size, log=log)
        reset_sequences(target)
        problems = verify_copy(source, target, report)
        for problem in problems:
            log(f"[FAIL] {problem}")
        if problems:
            log("the target is partially populated; drop it and rerun (is the source still being written to?)")
            return 1
        log("verified: every table's row count and primary keys match the source")
        log("NOT copied: the per-user memory store (BESTTEAM_MEMORY_DB) and the files under "
            "ui/backend/data (uploads, knowledge-base versions) -- they stay where they are. "
            "To switch the backend over, set BESTTEAM_DATABASE_URL to the target and restart.")
        return 0
    finally:
        source.dispose()
        if target is not None:
            target.dispose()
