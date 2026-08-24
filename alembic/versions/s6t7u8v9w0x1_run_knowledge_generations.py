"""run_knowledge_generations: which KB generation a run's trace names

Revision ID: s6t7u8v9w0x1
Revises: r5s6t7u8v9w0
Create Date: 2026-08-24 00:00:00.000000

A knowledge-base search leaves `ingestion_job_id` and per-hit `chunk_id`s in
the run's trace (PR #86), and generation pruning deleted those rows two
uploads later. This table is the reference that stops the prune: a
generation some un-purged run names keeps its document/chunk rows (vectors
nulled, files deleted). No backfill -- generations already pruned are gone;
from here on a referenced one is never pruned while its reference stands.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 's6t7u8v9w0x1'
down_revision: Union[str, Sequence[str], None] = 'r5s6t7u8v9w0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "run_knowledge_generations"


def upgrade() -> None:
    """Guarded (create_all-at-import idempotency, same as the other
    migrations): a database booted by the backend before `alembic upgrade
    head` already has the table."""
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column(
            "ingestion_job_id", sa.Integer(),
            sa.ForeignKey("knowledge_ingestion_jobs.id"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "run_id", "ingestion_job_id",
            name="uq_run_knowledge_generations_run_id_job_id",
        ),
    )
    op.create_index(
        "ix_run_knowledge_generations_ingestion_job_id", _TABLE, ["ingestion_job_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_run_knowledge_generations_ingestion_job_id", table_name=_TABLE)
    op.drop_table(_TABLE)
