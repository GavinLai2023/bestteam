"""add users.security_stamp

Revision ID: a7b8c9d0e1f2
Revises: f3a4b5c6d7e8
Create Date: 2026-07-27 00:00:00.000000

"""
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f3a4b5c6d7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add `users.security_stamp` (per-account credential generation).

    Guarded (create_all-at-import idempotency, same as the other migrations).
    Each existing user is backfilled with a fresh random stamp; their tokens
    predate the column (no `sec` claim) so they re-authenticate once -- the
    intended one-time effect of this security hardening.
    """
    inspector = sa.inspect(op.get_bind())
    has_col = any(col["name"] == "security_stamp" for col in inspector.get_columns("users"))
    if not has_col:
        op.add_column('users', sa.Column('security_stamp', sa.String(), nullable=True))
        conn = op.get_bind()
        rows = conn.execute(sa.text("SELECT id FROM users")).fetchall()
        for (uid,) in rows:
            conn.execute(
                sa.text("UPDATE users SET security_stamp = :s WHERE id = :i"),
                {"s": secrets.token_hex(16), "i": uid},
            )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'security_stamp')
