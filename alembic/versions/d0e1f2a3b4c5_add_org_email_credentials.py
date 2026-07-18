"""add org_email_credentials (per-org mailbox connection)

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-18 12:00:00.000000

Creates the per-org email credential store: one encrypted mailbox connection
per organization, so a multi-org deployment can use the email tools (the
process-wide BESTTEAM_EMAIL_* path is single-mailbox and refused on multi-org).
`password_encrypted` holds a Fernet token, never plaintext.

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has the table when
this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "org_email_credentials"):
        op.create_table(
            "org_email_credentials",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("backend", sa.String(), nullable=False, server_default="imap"),
            sa.Column("host", sa.String(), nullable=False),
            sa.Column("port", sa.Integer(), nullable=False, server_default="993"),
            sa.Column("username", sa.String(), nullable=False),
            sa.Column("password_encrypted", sa.String(), nullable=False),
            sa.Column("drafts_folder", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("org_id", name="uq_org_email_credentials_org_id"),
        )


def downgrade() -> None:
    """Downgrade schema (drops all stored per-org mailbox credentials)."""
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "org_email_credentials"):
        op.drop_table("org_email_credentials")
