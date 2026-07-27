"""add users.password_changed_at

Revision ID: a7b8c9d0e1f2
Revises: f3a4b5c6d7e8
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f3a4b5c6d7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add `users.password_changed_at` (token revocation anchor).

    Guarded (create_all-at-import idempotency, same as the other migrations).
    Nullable with no default: existing users have never had a reset, so every
    already-issued token stays valid until it expires normally.
    """
    inspector = sa.inspect(op.get_bind())
    has_col = any(col["name"] == "password_changed_at" for col in inspector.get_columns("users"))
    if not has_col:
        op.add_column('users', sa.Column('password_changed_at', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'password_changed_at')
