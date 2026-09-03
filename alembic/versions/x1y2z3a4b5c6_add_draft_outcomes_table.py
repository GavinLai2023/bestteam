"""draft_outcomes: what the mailbox eventually did with each platform draft

Revision ID: x1y2z3a4b5c6
Revises: w0x1y2z3a4b5
Create Date: 2026-09-03 00:00:00.000000

One row per confirmed platform-written draft; the email-trigger poll cycle
reconciles rows against the mailbox (Drafts gone? found in Sent?) and the
Automations tab shows the counts. See docs/superpowers/specs/
2026-09-03-draft-outcome-tracking-design.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'x1y2z3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'w0x1y2z3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "draft_outcomes"


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
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("evidence", sa.String(), nullable=True),
        sa.Column("origin_message_id", sa.String(), nullable=True),
        sa.Column("miss_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("source_key", name="uq_draft_outcomes_source_key"),
    )
    op.create_index("ix_draft_outcomes_org_id_status", _TABLE, ["org_id", "status"])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_draft_outcomes_org_id_status", table_name=_TABLE)
    op.drop_table(_TABLE)
