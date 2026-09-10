"""knowledge_ingestion_jobs: add parser_revision

Revision ID: b5c6d7e8f9g0
Revises: a4b5c6d7e8f9
Create Date: 2026-08-23 00:00:00.000000

Incremental ingestion carries an unchanged document's chunks forward from the
previous completed job, matched on the sha256 of the file's raw bytes. That
hash is blind to a change in the code that turns those bytes into text: after
the XML renderer started dropping the OMG diagram-interchange namespaces
(BPMN/DMN layout geometry -- 48% of an exported process diagram), a customer
re-uploading the same files would have kept the old, coordinate-heavy chunks
forever and nothing would have said why.

`_carryable` now also requires the previous job's `parser_revision` to equal
the running code's. Nullable, no backfill: a job written before this migration
cannot say which parser cut its chunks, and unknown is treated the way a NULL
`chunk_size` already is -- not reusable. The first upload after an upgrade
re-cuts once; every one after that is incremental again.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5c6d7e8f9g0'
down_revision: Union[str, Sequence[str], None] = 'a4b5c6d7e8f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "knowledge_ingestion_jobs"
_COLUMN = "parser_revision"


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Add the parser generation to an ingestion job.

    Guarded (create_all-at-import idempotency, same as the other migrations):
    a database booted by the backend before `alembic upgrade head` already
    has the column.
    """
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    if not _has_column(bind, _TABLE, _COLUMN):
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    if _has_column(bind, _TABLE, _COLUMN):
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
