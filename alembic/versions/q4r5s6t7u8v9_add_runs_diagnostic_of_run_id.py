"""runs: add diagnostic_of_run_id

Revision ID: q4r5s6t7u8v9
Revises: p3q4r5s6t7u8
Create Date: 2026-08-21 00:00:00.000000

An admin's diagnostic re-run of a poor run (`POST /api/runs/{id}/diagnose`)
is a new `runs` row that points back at the run it diagnoses -- the same
shape as `retry_of_run_id`. Nullable, no backfill: every existing row is an
ordinary run. See docs/superpowers/specs/2026-08-21-diagnostic-rerun-design.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'q4r5s6t7u8v9'
down_revision: Union[str, Sequence[str], None] = 'p3q4r5s6t7u8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Add `runs.diagnostic_of_run_id`.

    Guarded (create_all-at-import idempotency, same as the other migrations):
    a database booted by the backend before `alembic upgrade head` already
    has the column.
    """
    bind = op.get_bind()
    if "runs" not in sa.inspect(bind).get_table_names():
        return
    if not _has_column(bind, "runs", "diagnostic_of_run_id"):
        with op.batch_alter_table("runs") as batch:
            batch.add_column(sa.Column("diagnostic_of_run_id", sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "runs", "diagnostic_of_run_id"):
        with op.batch_alter_table("runs") as batch:
            batch.drop_column("diagnostic_of_run_id")
