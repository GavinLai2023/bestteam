"""add organizations.active

Revision ID: f3a4b5c6d7e8
Revises: d4e6b2c9f1a7
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, Sequence[str], None] = 'd4e6b2c9f1a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add `organizations.active` (reversible org deactivation).

    Guarded (create_all-at-import idempotency, same as the other migrations): if
    the backend booted first the column already exists, so re-adding it would
    fail. The server_default backfills every existing org to active, so an
    upgrade suspends nothing.
    """
    inspector = sa.inspect(op.get_bind())
    has_active = any(col["name"] == "active" for col in inspector.get_columns("organizations"))
    if not has_active:
        op.add_column(
            'organizations',
            sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('organizations', 'active')
