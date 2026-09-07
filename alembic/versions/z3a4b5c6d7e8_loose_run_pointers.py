"""inbox_events.run_id / email_triggers.last_run_id stop being foreign keys

Revision ID: z3a4b5c6d7e8
Revises: y2z3a4b5c6d7
Create Date: 2026-09-07 00:00:00.000000

Both columns are written BEFORE the `runs` row they name exists, by design:
`email_trigger._start_triggered_run` commits the claim (`inbox_events.run_id`)
before any pipeline build is attempted, so a build failure hands the messages
back penalty-free, and its dispatch compare-and-swap writes
`email_triggers.last_run_id` in the statement before the row is inserted --
`runtime._release_orphaned_claims` exists precisely to reconcile the claims a
crash leaves in that window. SQLite never enforced the keys, so the modelling
error was invisible; Postgres always enforces, and would refuse every
autonomous email run. They are loose pointers, so the constraints go.

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, and the current models declare no such keys, so a
fresh database has nothing to drop.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'z3a4b5c6d7e8'
down_revision: Union[str, Sequence[str], None] = 'y2z3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQLite writes these constraints unnamed, and an unnamed constraint cannot be
# dropped. Reflecting the table under this convention gives the batch copy a
# name to address; Postgres named them itself at CREATE TABLE time.
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}

_POINTERS = (("inbox_events", "run_id"), ("email_triggers", "last_run_id"))


def _name_for(table: str, column: str) -> str:
    return f"fk_{table}_{column}_runs"


def _fk_on(bind, table: str, column: str):
    """The reflected foreign key on `table.column`, or None if there is none."""
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return None
    return next(
        (fk for fk in inspector.get_foreign_keys(table)
         if fk["constrained_columns"] == [column]),
        None,
    )


def _drop_fk(table: str, column: str) -> None:
    bind = op.get_bind()
    fk = _fk_on(bind, table, column)
    if fk is None:
        return  # create_all from the current models never declared it
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table, naming_convention=_NAMING) as batch_op:
            batch_op.drop_constraint(_name_for(table, column), type_="foreignkey")
    else:
        op.drop_constraint(fk["name"], table, type_="foreignkey")


def _create_fk(table: str, column: str) -> None:
    bind = op.get_bind()
    if table not in sa.inspect(bind).get_table_names():
        return
    if _fk_on(bind, table, column) is not None:
        return
    name = _name_for(table, column)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table, naming_convention=_NAMING) as batch_op:
            batch_op.create_foreign_key(name, "runs", [column], ["id"])
    else:
        op.create_foreign_key(name, table, "runs", [column], ["id"])


def upgrade() -> None:
    """Upgrade schema."""
    for table, column in _POINTERS:
        _drop_fk(table, column)


def downgrade() -> None:
    """Downgrade schema.

    Restores the constraints under an explicit name rather than the anonymous
    one SQLite/Postgres generated originally -- a foreign key's name is a
    diagnostic label here, never queried against.
    """
    for table, column in _POINTERS:
        _create_fk(table, column)
