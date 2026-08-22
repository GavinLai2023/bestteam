"""knowledge_ingestion_jobs: add chunk_size / chunk_overlap

Revision ID: r5s6t7u8v9w0
Revises: q4r5s6t7u8v9
Create Date: 2026-08-22 00:00:00.000000

Incremental ingestion carries an unchanged document's chunks (and their
embeddings) forward from the previous completed job instead of re-parsing and
re-embedding them. That is only sound when the new job would have cut the same
chunks, so the job has to record the parameters it was cut with -- the same
reason `kb_type`/`embedding_model` are already on this row rather than read
back off the KnowledgeBaseRecord, whose `config` has already advanced to the
new upload's spec by the time the worker runs.

Nullable, no backfill: a job written before this migration cannot say what it
used, and `_carryable` treats unknown as "not reusable", so the first upload
after an upgrade re-embeds once and every one after that is incremental.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'r5s6t7u8v9w0'
down_revision: Union[str, Sequence[str], None] = 'q4r5s6t7u8v9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "knowledge_ingestion_jobs"
_COLUMNS = ("chunk_size", "chunk_overlap")


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Add the two chunking parameters to an ingestion job.

    Guarded (create_all-at-import idempotency, same as the other migrations):
    a database booted by the backend before `alembic upgrade head` already
    has the columns.
    """
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    with op.batch_alter_table(_TABLE) as batch:
        for column in _COLUMNS:
            if not _has_column(bind, _TABLE, column):
                batch.add_column(sa.Column(column, sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    with op.batch_alter_table(_TABLE) as batch:
        for column in _COLUMNS:
            if _has_column(bind, _TABLE, column):
                batch.drop_column(column)
