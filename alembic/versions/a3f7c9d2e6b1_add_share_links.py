"""add share_links, share_sessions, share_messages (anonymous team sharing)

Revision ID: a3f7c9d2e6b1
Revises: 5e806924cfec
Create Date: 2026-08-14 12:00:00.000000

Three tables backing anonymous, revocable colleague access to one deployed
team with continuous chat. See
docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md.

Guarded ops (same reason as every other migration here):
`ui/backend/db_session.py` runs `create_all` at import, so a fresh database
already has these tables when this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f7c9d2e6b1'
down_revision: Union[str, Sequence[str], None] = '5e806924cfec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "share_links"):
        op.create_table(
            "share_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workflow_id", sa.Integer(), sa.ForeignKey("workflows.id"), nullable=False),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("token", sa.String(), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("daily_cap", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("token", name="uq_share_links_token"),
        )
    if not _has_table(inspector, "share_sessions"):
        op.create_table(
            "share_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("share_link_id", sa.Integer(), sa.ForeignKey("share_links.id"), nullable=False),
            sa.Column("session_token", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("last_active_at", sa.DateTime(), nullable=True),
            sa.Column("turns_today", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("turns_date", sa.String(), nullable=True),
            sa.UniqueConstraint("session_token", name="uq_share_sessions_session_token"),
        )
    if not _has_table(inspector, "share_messages"):
        op.create_table(
            "share_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("share_session_id", sa.Integer(), sa.ForeignKey("share_sessions.id"), nullable=False),
            sa.Column("turn_number", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("content", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "share_session_id", "turn_number", name="uq_share_messages_session_turn"
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "share_messages"):
        op.drop_table("share_messages")
    if _has_table(inspector, "share_sessions"):
        op.drop_table("share_sessions")
    if _has_table(inspector, "share_links"):
        op.drop_table("share_links")
