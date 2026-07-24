"""add users.is_admin

Revision ID: a1b2c3d4e5f6
Revises: 884e80106da7
Create Date: 2026-07-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '884e80106da7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Guarded (create_all-at-import idempotency, same as the baseline): if the
    backend booted first, `users.is_admin` already exists, so re-adding it would
    fail. Add only when the column is missing.
    """
    inspector = sa.inspect(op.get_bind())
    has_is_admin = any(col["name"] == "is_admin" for col in inspector.get_columns("users"))
    if not has_is_admin:
        op.add_column(
            'users',
            sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'is_admin')
