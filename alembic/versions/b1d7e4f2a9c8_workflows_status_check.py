"""constrain workflows.status + backfill legacy rows to deployed

Revision ID: b1d7e4f2a9c8
Revises: 57b13700d5df
Create Date: 2026-07-24 00:00:00.000000

Only `status='deployed'` workflows are runnable/listed now (P1-06). Existing
rows were runnable regardless of status, so backfill non-deployed -> deployed to
preserve behavior on upgrade. Then add a CHECK bounding the column.

Guarded (create_all-at-import idempotency): the model now declares the CHECK, so
a fresh create_all database already has `ck_workflows_status`; add it only when
absent, matching the other migrations' inspection guards.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1d7e4f2a9c8'
down_revision: Union[str, Sequence[str], None] = '57b13700d5df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALLOWED = "status IN ('draft', 'ready_for_testing', 'deployed')"


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("UPDATE workflows SET status = 'deployed' WHERE status != 'deployed'")
    existing = {c["name"] for c in sa.inspect(bind).get_check_constraints("workflows")}
    if "ck_workflows_status" not in existing:
        with op.batch_alter_table("workflows") as batch:
            batch.create_check_constraint("ck_workflows_status", _ALLOWED)


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints("workflows")}
    if "ck_workflows_status" in existing:
        with op.batch_alter_table("workflows") as batch:
            batch.drop_constraint("ck_workflows_status", type_="check")
