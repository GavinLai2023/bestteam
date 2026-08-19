"""usage_records: nullable run_id + ingestion_job_id (KB beta P0-4)

Revision ID: n1o2p3q4r5s6
Revises: m0n1o2p3q4r5
Create Date: 2026-08-18 12:00:00.000000

A knowledge base's ingestion embeddings are billed to an upload, not to a
run, so they need a home in the one usage ledger the org's monthly spend cap
sums over. Rather than a second table, `run_id` becomes nullable and a
nullable `ingestion_job_id` FK says which upload the spend came from. A row
therefore names one of three sources: a run (`run_id` set), an ingestion job
(`ingestion_job_id` set), or neither -- an ad-hoc `agent="kb:search"` test
search, which belongs to no run and no upload and so leaves both NULL.

Existing rows are untouched -- every one of them belongs to a run and keeps
its `run_id`. SQLite cannot ALTER a column's nullability in place, so both
changes go through one `batch_alter_table` (move-and-copy) pass, and every op
is inspection-guarded because `db_session.py` runs `create_all` at import:
a freshly booted deployment already matches head here (see `b7c8d9e0f1a2`).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'n1o2p3q4r5s6'
down_revision: Union[str, Sequence[str], None] = 'm0n1o2p3q4r5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "usage_records"
_JOB_COLUMN = "ingestion_job_id"


def _columns(inspector, table: str) -> dict:
    if table not in inspector.get_table_names():
        return {}
    return {column["name"]: column for column in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    columns = _columns(inspector, _TABLE)
    if not columns:
        return  # create_all hasn't run and the table isn't here yet

    make_nullable = not columns["run_id"]["nullable"]
    add_job_column = _JOB_COLUMN not in columns
    if not (make_nullable or add_job_column):
        return

    with op.batch_alter_table(_TABLE) as batch:
        if make_nullable:
            batch.alter_column("run_id", existing_type=sa.String(), nullable=True)
        if add_job_column:
            batch.add_column(sa.Column(_JOB_COLUMN, sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_usage_records_ingestion_job_id",
                "knowledge_ingestion_jobs",
                [_JOB_COLUMN],
                ["id"],
            )


def downgrade() -> None:
    """Downgrade schema.

    Lossy by design: an ingestion row has no `run_id` to restore, so it cannot
    survive a NOT NULL `run_id`. Those rows are deleted rather than given a
    fabricated run id -- deleting spend the old schema has no way to express
    is the honest option, and the ledger's other rows are untouched.
    """
    inspector = sa.inspect(op.get_bind())
    columns = _columns(inspector, _TABLE)
    if not columns:
        return

    op.execute(f"DELETE FROM {_TABLE} WHERE run_id IS NULL")
    with op.batch_alter_table(_TABLE) as batch:
        if _JOB_COLUMN in columns:
            batch.drop_column(_JOB_COLUMN)
        if columns["run_id"]["nullable"]:
            batch.alter_column("run_id", existing_type=sa.String(), nullable=False)
