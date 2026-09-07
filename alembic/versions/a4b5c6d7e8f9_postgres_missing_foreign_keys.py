"""declare on Postgres the five foreign keys the chain never carried

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-09-07 00:00:00.000000

Five columns the models declare as foreign keys were added to existing tables
by a bare `batch.add_column(sa.Column(...))` that never carried the
`ForeignKey`: `runs.pipeline_version_id`, `pipelines.current_version_id` and
`builder_sessions.pipeline_id` in `c3f5a1b8e2d4`, `runs.retry_of_run_id` in
`c1d2e3f4a5b6`, and `runs.diagnostic_of_run_id` in `q4r5s6t7u8v9`. SQLite
never enforces a key, so the omission was invisible until the Postgres lane
compared a chain-migrated schema against `create_all`'s
(`tests/test_migrations_postgres.py`) and found 45 keys where the models
declare 50. The chain is production's Postgres schema (Ruling 6), so it has
to declare them. `c4d5e6f7a8b9` already does exactly this repair for
`skills.current_version_id` and `pipeline_dependencies.resource_version_id`;
these five are the remainder.

**SQLite is deliberately skipped, and this migration is a no-op there.** The
production SQLite file keeps foreign-key enforcement off, so a key declared
on it is never checked by anything -- and a real SQLite deployment already
has all five anyway, because the documented deploy order runs the backend
(hence `Base.metadata.create_all`, which builds from the models) before
`alembic upgrade head`. Only a chain-only database lacks them, and the sole
chain-only databases are this suite's throwaway files. Adding a key on SQLite
means a batch rewrite copying every row of `runs` -- the largest table in a
deployment -- plus `pipelines` and `builder_sessions`, which buys nothing
that is ever enforced.

`ADD CONSTRAINT` validates the rows already there, so on a populated database
a dangling pointer refuses the upgrade. That is the correct behaviour for a
schema that claims the key, and the cutover builds its Postgres fresh.

The inspector guard keeps this a no-op on a `create_all`-built Postgres,
which already has all five.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e8f9'
down_revision: Union[str, Sequence[str], None] = 'z3a4b5c6d7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEYS = (
    # (table, column, referred_table, referred_column)
    ("runs", "pipeline_version_id", "pipeline_versions", "id"),
    ("runs", "retry_of_run_id", "runs", "id"),
    ("runs", "diagnostic_of_run_id", "runs", "id"),
    ("pipelines", "current_version_id", "pipeline_versions", "id"),
    ("builder_sessions", "pipeline_id", "pipelines", "id"),
)


def _name(table: str, column: str, referred_table: str) -> str:
    return f"fk_{table}_{column}_{referred_table}"


def _fk_on(inspector, table: str, column: str):
    """The reflected foreign key on `table.column`, or None if there is none."""
    return next(
        (fk for fk in inspector.get_foreign_keys(table)
         if fk["constrained_columns"] == [column]),
        None,
    )


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return  # see the module docstring
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, column, referred_table, referred_column in _KEYS:
        if table in tables and _fk_on(inspector, table, column) is None:
            op.create_foreign_key(
                _name(table, column, referred_table),
                table, referred_table, [column], [referred_column],
            )


def downgrade() -> None:
    """Downgrade schema.

    Drops whatever key currently sits on each column, by its reflected name --
    a `create_all`-built database named them `<table>_<column>_fkey` rather
    than by the metadata convention this migration uses.
    """
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, column, _referred_table, _referred_column in _KEYS:
        fk = _fk_on(inspector, table, column) if table in tables else None
        if fk is not None:
            op.drop_constraint(fk["name"], table, type_="foreignkey")
