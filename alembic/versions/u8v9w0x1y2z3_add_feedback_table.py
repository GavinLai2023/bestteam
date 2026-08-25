"""feedback: defect reports and suggestions for the platform operator

Revision ID: u8v9w0x1y2z3
Revises: t7u8v9w0x1y2
Create Date: 2026-08-26 00:00:00.000000

One row per submission, from a logged-in user (`submitted_by`) or a
share-link visitor (`share_session_id`); triage is `status`/`admin_note`
edited on the admin Feedback page. See docs/superpowers/specs/
2026-08-26-feedback-system-design.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'u8v9w0x1y2z3'
down_revision: Union[str, Sequence[str], None] = 't7u8v9w0x1y2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "feedback"


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
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("admin_note", sa.String(), nullable=True),
        sa.Column("submitted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "share_session_id", sa.Integer(), sa.ForeignKey("share_sessions.id"), nullable=True
        ),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("kind IN ('defect', 'suggestion')", name="ck_feedback_kind"),
        sa.CheckConstraint(
            "status IN ('new', 'acknowledged', 'resolved', 'dismissed')",
            name="ck_feedback_status",
        ),
    )
    op.create_index("ix_feedback_share_session_id", _TABLE, ["share_session_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_feedback_share_session_id", table_name=_TABLE)
    op.drop_table(_TABLE)
