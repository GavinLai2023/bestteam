"""add email_triggers (autonomous new-mail trigger state)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-19 12:00:00.000000

One row per org: the customer's opt-in for autonomous email-triggered runs,
plus poller state (UID dedup baseline, daily-cap counters, health).

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has the table when
this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "email_triggers"):
        op.create_table(
            "email_triggers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("workflow_name", sa.String(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_uid", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("uidvalidity", sa.Integer(), nullable=True),
            sa.Column("runs_today", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("runs_date", sa.String(), nullable=True),
            sa.Column("last_run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=True),
            sa.Column("last_checked_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("org_id", name="uq_email_triggers_org_id"),
        )


def downgrade() -> None:
    """Downgrade schema (drops all trigger opt-ins and poller state)."""
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "email_triggers"):
        op.drop_table("email_triggers")
