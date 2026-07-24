"""drop vestigial agents/teams tables

Revision ID: 57b13700d5df
Revises: e85b2230b950
Create Date: 2026-07-24 00:00:00.000000

`AgentRecord`/`TeamRecord` are vestigial: no runtime loader ever read them --
a Workflow carries its agents/teams inline in its own `config`, so a standalone
row could never reach a run (see `ui/backend/db/CLAUDE.md`). Their `/api/config`
CRUD routes were removed in `036e1d6`; both tables have been empty in every
deployment we operate.

However, *writable* CRUD routes for these tables did exist historically
(`78c7a8a`..`036e1d6`), so we can't assume every deployment is empty. This
migration therefore **refuses** rather than silently destroying data: if either
table holds rows it raises with export guidance, and only drops when they are
empty. A drop is irreversible (downgrade recreates the tables empty).

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so the tables may or may not be present when this
runs; each drop is guarded by inspection.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '57b13700d5df'
down_revision: Union[str, Sequence[str], None] = 'e85b2230b950'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()

    populated = []
    for name in ("agents", "teams"):
        if name in tables:
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {name}")).scalar()
            if count:
                populated.append((name, count))
    if populated:
        detail = ", ".join(f"{name} ({count} rows)" for name, count in populated)
        raise RuntimeError(
            f"Refusing to drop legacy tables that contain data: {detail}. "
            "These tables are being removed as vestigial, but this database has "
            "rows in them (a deployment that used the old /api/config/agents or "
            "/api/config/teams routes). Export or back up the data, then drop the "
            "tables manually before re-running `alembic upgrade head`."
        )

    for name in ("agents", "teams"):
        if name in tables:
            op.drop_table(name)


def downgrade() -> None:
    """Downgrade schema (recreates the empty agents/teams tables)."""
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()
    for table_name in ("agents", "teams"):
        if table_name in tables:
            continue
        op.create_table(
            table_name,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("org_id", sa.Integer(), nullable=True),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("org_id", "name", name=f"uq_{table_name}_org_id_name"),
            sa.ForeignKeyConstraint(
                ["org_id"], ["organizations.id"], name=f"fk_{table_name}_org_id_organizations"
            ),
        )
