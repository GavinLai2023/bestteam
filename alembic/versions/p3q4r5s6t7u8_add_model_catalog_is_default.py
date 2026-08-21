"""model_catalog: add is_default

Revision ID: p3q4r5s6t7u8
Revises: o2p3q4r5s6t7
Create Date: 2026-08-20 00:00:00.000000

The Solution Architect's model was picked implicitly by alphabetically
sorting `spec` strings (`pickDefaultModel` in the frontend), with no way for
an operator to choose it. `is_default` lets an admin mark one catalog entry
as that default; `server_default=false()` backfills every existing row to
"not default", so an upgrade changes no deployment's behaviour until an
operator explicitly sets one. At most one row is ever True -- enforced in
`ui/backend/db/model_catalog.py::upsert_entry`, not by a DB constraint.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'p3q4r5s6t7u8'
down_revision: Union[str, Sequence[str], None] = 'o2p3q4r5s6t7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add `model_catalog.is_default`.

    Guarded (create_all-at-import idempotency, same as the other migrations):
    a database booted by the backend before `alembic upgrade head` already
    has the column.
    """
    inspector = sa.inspect(op.get_bind())
    if "model_catalog" not in inspector.get_table_names():
        return
    has_column = any(col["name"] == "is_default" for col in inspector.get_columns("model_catalog"))
    if not has_column:
        op.add_column(
            'model_catalog',
            sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('model_catalog', 'is_default')
