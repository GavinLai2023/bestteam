"""usage_records.ingestion_job_id stops being a foreign key

Revision ID: c6d7e8f9g0h1
Revises: b5c6d7e8f9g0
Create Date: 2026-09-11 00:00:00.000000

The column is a provenance label: one `agent="kb:ingest"` row per completed
upload, naming the job that caused the embedding spend. The job row it names
is deleted by design -- `ingestion._prune_old_ingestion_versions` keeps only
the two newest completed generations, and deleting a knowledge base deletes
every job it ever ran -- while the usage row must survive both, because it is
the org's cost history ("keep the accounting", the rule retention follows).
Declared as a foreign key, the two cannot both hold. SQLite never enforced it,
so the prune left a dangling id and nobody noticed; Postgres enforces it, so
from the first billed upload the prune fails on every later upload (the rows
of every old generation accumulate, the failure logged and swallowed) and
deleting the knowledge base is refused outright. Same treatment as
`inbox_events.run_id` (z3a4b5c6d7e8): the pointer stays, the constraint goes.

`downgrade` nulls any pointer that names a deleted job first -- a constraint
the data violates cannot be re-added, and the id of a pruned job resolves to
nothing anyway.

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, and the current models declare no such key, so a
fresh database has nothing to drop.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c6d7e8f9g0h1'
down_revision: Union[str, Sequence[str], None] = 'b5c6d7e8f9g0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "usage_records"
_COLUMN = "ingestion_job_id"
_PARENT = "knowledge_ingestion_jobs"

# An unnamed constraint cannot be dropped. `create_all` on SQLite writes this
# one unnamed, so the batch copy reflects the table under this convention to
# have a name to address; the chain (`n1o2p3q4r5s6`) and Postgres named it
# themselves, and a reflected name is used as found.
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
_NAME = f"fk_{_TABLE}_{_COLUMN}_{_PARENT}"


def _fk(bind):
    """The reflected foreign key on the column, or None if there is none."""
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return None
    return next(
        (fk for fk in inspector.get_foreign_keys(_TABLE)
         if fk["constrained_columns"] == [_COLUMN]),
        None,
    )


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    fk = _fk(bind)
    if fk is None:
        return  # create_all from the current models never declared it
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, naming_convention=_NAMING) as batch_op:
            batch_op.drop_constraint(fk["name"] or _NAME, type_="foreignkey")
    else:
        op.drop_constraint(fk["name"], _TABLE, type_="foreignkey")


def downgrade() -> None:
    """Downgrade schema.

    The key is restored only after the values that would violate it are
    cleared, because `ALTER TABLE ... ADD CONSTRAINT` validates the existing
    rows -- on Postgres one pruned generation's spend would otherwise refuse
    the whole downgrade with a ForeignKeyViolation. Restored under an explicit
    name rather than the anonymous one the engine generated originally; a
    foreign key's name is a diagnostic label here, never queried against.
    """
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    bind.execute(sa.text(
        f"UPDATE {_TABLE} SET {_COLUMN} = NULL "
        f"WHERE {_COLUMN} IS NOT NULL AND {_COLUMN} NOT IN (SELECT id FROM {_PARENT})"
    ))
    if _fk(bind) is not None:
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, naming_convention=_NAMING) as batch_op:
            batch_op.create_foreign_key(_NAME, _PARENT, [_COLUMN], ["id"])
    else:
        op.create_foreign_key(_NAME, _TABLE, _PARENT, [_COLUMN], ["id"])
