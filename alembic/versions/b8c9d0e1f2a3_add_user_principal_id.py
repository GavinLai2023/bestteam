"""add users.principal_id

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-30 00:00:00.000000

"""
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add `users.principal_id` (immutable per-account memory principal).

    Guarded (create_all-at-import idempotency, same as the other migrations).
    Each existing user is backfilled with a fresh random immutable principal, so
    every current account has a non-null principal immediately. Their existing
    memory rows (a separate SQLite store) stay principal-NULL and so aren't
    recalled by a stamped run -- the SP-2 accept-legacy precedent; the optional
    `backfill-memory-principals` CLI reconciles them for deployments that want
    existing memory to keep being recalled.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    has_col = any(col["name"] == "principal_id" for col in inspector.get_columns("users"))
    if not has_col:
        op.add_column('users', sa.Column('principal_id', sa.String(), nullable=True))
    # Backfill EVERY NULL principal, not only on the run that added the column: an
    # interrupted upgrade can commit the column-add (or a create_all bootstrap can
    # create the column) and then die before backfilling. Gating the backfill on
    # "did we just add it" would permanently leave those rows NULL, disabling
    # principal scoping + the retirement fence for them (finding 2). Idempotent:
    # a second run finds no NULLs and does nothing.
    rows = conn.execute(sa.text("SELECT id FROM users WHERE principal_id IS NULL")).fetchall()
    for (uid,) in rows:
        conn.execute(
            sa.text("UPDATE users SET principal_id = :p WHERE id = :i"),
            {"p": secrets.token_hex(16), "i": uid},
        )
    # Fail loudly rather than silently leaving a NULL principal that would disable
    # the fence for that account.
    remaining = conn.execute(
        sa.text("SELECT COUNT(*) FROM users WHERE principal_id IS NULL")
    ).scalar()
    if remaining:
        raise RuntimeError(
            f"principal_id backfill left {remaining} NULL row(s); refusing to complete."
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'principal_id')
