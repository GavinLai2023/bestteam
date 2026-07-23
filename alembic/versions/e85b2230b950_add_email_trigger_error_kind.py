"""add email_triggers.last_error_kind (mailbox vs workflow health)

Revision ID: e85b2230b950
Revises: f2a3b4c5d6e7
Create Date: 2026-07-22 09:00:00.000000

Separates a mailbox-connectivity fault (auto-clears on the next successful
check) from a workflow/dispatch fault (persists until a real successful
dispatch). NULL on existing rows -- treated as sticky (today's behavior),
same as an unrecognized kind.

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has the column when
this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e85b2230b950'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, "email_triggers", "last_error_kind"):
        op.add_column("email_triggers", sa.Column("last_error_kind", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema (drops the mailbox/workflow error distinction)."""
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "email_triggers", "last_error_kind"):
        op.drop_column("email_triggers", "last_error_kind")
