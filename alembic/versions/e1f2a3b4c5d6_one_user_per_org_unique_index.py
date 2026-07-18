"""enforce one member per org (partial unique index on users.org_id)

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-18 14:00:00.000000

Makes "one member per org" a schema invariant, not just a `create_user` check:
a partial unique index on `users.org_id WHERE org_id IS NOT NULL`. Platform
operators (org_id NULL) are excluded, so there can still be many of them.

Upgrade policy for pre-existing data: a database created under the earlier
multi-member architecture may already have several members in one org. We do
NOT delete or reassign accounts (destructive); instead the migration refuses
with a clear message naming the affected orgs so the operator resolves them
by hand (e.g. `admin` CLI) before re-running.

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has the index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "uq_users_org_id_not_null"


def _has_index(inspector, table: str, index: str) -> bool:
    return any(ix["name"] == index for ix in inspector.get_indexes(table))


def duplicate_org_members(bind) -> list[tuple]:
    """Return `(org_id, count)` rows for orgs that have more than one member.

    Non-null org_id only -- platform operators (NULL) are allowed to be many.
    Module-level so the upgrade policy is unit-testable without an alembic env.
    """
    return list(
        bind.execute(
            sa.text(
                "SELECT org_id, COUNT(*) AS n FROM users "
                "WHERE org_id IS NOT NULL GROUP BY org_id HAVING n > 1"
            )
        ).fetchall()
    )


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return  # brand-new database; create_all builds the index directly
    if _has_index(inspector, "users", _INDEX_NAME):
        return

    duplicates = duplicate_org_members(bind)
    if duplicates:
        offending = ", ".join(f"org_id={row[0]} ({row[1]} members)" for row in duplicates)
        raise RuntimeError(
            "Cannot enforce one member per org: these orgs already have multiple "
            f"members: {offending}. Resolve them before running this migration -- "
            "accounts are not deleted automatically. Use the operator CLI: "
            "`admin delete-user <name>` to remove an extra account, or "
            "`admin move-user <name> --to-org <other> | --platform` to reassign it "
            "(see docs/deployment.md 'Recovering a legacy multi-member org')."
        )

    op.create_index(
        _INDEX_NAME,
        "users",
        ["org_id"],
        unique=True,
        sqlite_where=sa.text("org_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if "users" in inspector.get_table_names() and _has_index(inspector, "users", _INDEX_NAME):
        op.drop_index(_INDEX_NAME, table_name="users")
