"""add OAuth columns to org_email_credentials (email automation Phase 2)

Revision ID: i6j7k8l9m0n1
Revises: h5i6j7k8l9m0
Create Date: 2026-08-17 15:00:00.000000

Exchange Online no longer accepts basic auth, so a mailbox now records how it
authenticates. Purely additive: every existing row is a password mailbox,
which is the server default, so there is no backfill.

Guarded ops (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has these columns when
this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'i6j7k8l9m0n1'
down_revision: Union[str, Sequence[str], None] = 'h5i6j7k8l9m0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "org_email_credentials"


def _columns(inspector) -> set:
    if _TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    existing = _columns(inspector)
    if not existing:
        return
    if "auth_type" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("auth_type", sa.String(), nullable=False, server_default="password"),
        )
    if "oauth_tenant_id" not in existing:
        op.add_column(_TABLE, sa.Column("oauth_tenant_id", sa.String(), nullable=True))
    if "oauth_client_id" not in existing:
        op.add_column(_TABLE, sa.Column("oauth_client_id", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Plain DROP COLUMN: SQLite has supported it since 3.35, so no batch-mode
    table rebuild is needed.
    """
    inspector = sa.inspect(op.get_bind())
    existing = _columns(inspector)
    for name in ("oauth_client_id", "oauth_tenant_id", "auth_type"):
        if name in existing:
            op.drop_column(_TABLE, name)
