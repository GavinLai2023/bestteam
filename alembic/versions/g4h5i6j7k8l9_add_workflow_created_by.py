"""add workflows.created_by for user-level ownership tracking

Revision ID: g4h5i6j7k8l9
Revises: c1d2e3f4a5b6
Create Date: 2026-08-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'g4h5i6j7k8l9'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add `workflows.created_by` to track which user created each workflow.

    Guarded (create_all-at-import idempotency): if the backend booted first the
    column already exists, so re-adding it would fail. Existing workflows get NULL
    (unknown creator), which is safe since the column is nullable. New workflows
    created after this migration will have created_by set by the builder/admin.
    """
    inspector = sa.inspect(op.get_bind())
    has_created_by = any(col["name"] == "created_by" for col in inspector.get_columns("workflows"))
    if not has_created_by:
        op.add_column(
            'workflows',
            sa.Column('created_by', sa.String(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('workflows', 'created_by')
