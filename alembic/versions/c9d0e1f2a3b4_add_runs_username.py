"""add runs.username (initiating user, CR-032)

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-07-16 12:00:00.000000

Adds a nullable `username` column to `runs` recording who started the run,
so the initiator survives a restart/audit (`RunRegistry` is in-memory).
Informational only -- ownership stays org-level via `org_id`; legacy rows
stay NULL.

Guarded op (same reason as b7c8d9e0f1a2): `ui/backend/db_session.py` runs
`create_all` at import, so a fresh database already has the column when this
migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, "runs", "username"):
        op.add_column("runs", sa.Column("username", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema (drops who-started-it information)."""
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "runs", "username"):
        op.drop_column("runs", "username")
