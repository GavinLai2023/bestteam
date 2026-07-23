"""drop vestigial agents/teams tables

Revision ID: 57b13700d5df
Revises: e85b2230b950
Create Date: 2026-07-24 00:00:00.000000

`AgentRecord`/`TeamRecord` had no CRUD routes and no runtime loader ever read
them -- a Workflow carries its agents/teams inline in its own `config`, so a
standalone row could never reach a run (see `ui/backend/db/CLAUDE.md`). Safe
to drop unconditionally: nothing has ever written a row into either table.

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database created after the model
classes were removed never had these tables in the first place.
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
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()
    if "agents" in tables:
        op.drop_table("agents")
    if "teams" in tables:
        op.drop_table("teams")


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
