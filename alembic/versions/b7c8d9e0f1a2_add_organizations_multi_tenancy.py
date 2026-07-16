"""add organizations + org_id multi-tenancy columns

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-15 12:00:00.000000

Creates the `organizations` table, seeds the `default` org, adds nullable
`org_id` FKs to users and every org-owned table, swaps the five component
tables' unique(name) constraints for unique(org_id, name), and backfills
existing rows onto the default org (admins and built-in skills stay NULL).

SQLite notes: adding FKs / swapping unique constraints requires
`op.batch_alter_table` (move-and-copy). The original tables were created by
`create_all` with UNNAMED inline UNIQUE constraints, so each batch block
passes a `naming_convention` that lets Alembic's reflection assign them the
deterministic name `uq_<table>_<column>` that `drop_constraint` targets.

Guarded ops: `ui/backend/db_session.py` runs `create_all` at import, so by
the time this migration runs, a booted deployment already has the
`organizations` table (create_all adds missing TABLES but not missing
COLUMNS), and a fresh database already matches head entirely. Every op is
therefore guarded by inspection: create-if-absent, alter-if-column-missing.

Downgrade is lossy by design: org assignments are dropped and same-named
rows in different orgs would collide on the restored unique(name) -- only
downgrade a database that has a single org.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Names SQLite's reflected unnamed UNIQUE constraints so drop_constraint can
# target them (documented Alembic batch-mode recipe).
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}

# The five named component tables whose unique(name) becomes unique(org_id, name).
COMPONENT_TABLES = ("agents", "teams", "knowledge_bases", "skills", "workflows")
# Org-owned tables that just gain a nullable org_id FK.
OWNED_TABLES = ("builder_sessions", "runs", "usage_records")

# Built-in skills that stay in the platform tier (org_id NULL) on backfill.
# Snapshot of ui/backend/skills.py::DEFAULT_SKILLS at this revision's date.
BUILTIN_SKILL_NAMES = ("email_triage_reply",)

DEFAULT_ORG_SUBQUERY = "(SELECT id FROM organizations WHERE name = 'default')"


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())

    if "organizations" not in inspector.get_table_names():
        op.create_table(
            "organizations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False, unique=True),
            sa.Column("display_name", sa.String(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
    op.execute(
        "INSERT INTO organizations (name, display_name, created_at) "
        "SELECT 'default', 'Default Organization', CURRENT_TIMESTAMP "
        "WHERE NOT EXISTS (SELECT 1 FROM organizations WHERE name = 'default')"
    )

    if not _has_column(inspector, "users", "org_id"):
        with op.batch_alter_table("users", naming_convention=NAMING) as batch:
            batch.add_column(sa.Column("org_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_users_org_id_organizations", "organizations", ["org_id"], ["id"]
            )

    for table in COMPONENT_TABLES:
        if _has_column(inspector, table, "org_id"):
            continue  # fresh create_all database already matches head
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.add_column(sa.Column("org_id", sa.Integer(), nullable=True))
            batch.drop_constraint(f"uq_{table}_name", type_="unique")
            batch.create_unique_constraint(f"uq_{table}_org_id_name", ["org_id", "name"])
            batch.create_foreign_key(
                f"fk_{table}_org_id_organizations", "organizations", ["org_id"], ["id"]
            )

    for table in OWNED_TABLES:
        if _has_column(inspector, table, "org_id"):
            continue
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.add_column(sa.Column("org_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                f"fk_{table}_org_id_organizations", "organizations", ["org_id"], ["id"]
            )

    # Backfill AFTER the batch blocks so the UPDATEs hit the recreated tables.
    # Admins become platform operators (org NULL); everything else -> default.
    # Only NULL org_ids are touched, so rows that already carry an org (a
    # database written by post-multi-tenancy code) are never reassigned.
    op.execute(
        f"UPDATE users SET org_id = {DEFAULT_ORG_SUBQUERY} "
        "WHERE is_admin = 0 AND org_id IS NULL"
    )
    for table in ("agents", "teams", "knowledge_bases", "workflows") + OWNED_TABLES:
        op.execute(f"UPDATE {table} SET org_id = {DEFAULT_ORG_SUBQUERY} WHERE org_id IS NULL")
    builtin_list = ", ".join(f"'{name}'" for name in BUILTIN_SKILL_NAMES)
    op.execute(
        f"UPDATE skills SET org_id = {DEFAULT_ORG_SUBQUERY} "
        f"WHERE name NOT IN ({builtin_list}) AND org_id IS NULL"
    )


def downgrade() -> None:
    """Downgrade schema (lossy: drops org assignments; single-org DBs only)."""
    for table in OWNED_TABLES:
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.drop_constraint(f"fk_{table}_org_id_organizations", type_="foreignkey")
            batch.drop_column("org_id")

    for table in COMPONENT_TABLES:
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.drop_constraint(f"fk_{table}_org_id_organizations", type_="foreignkey")
            batch.drop_constraint(f"uq_{table}_org_id_name", type_="unique")
            batch.create_unique_constraint(f"uq_{table}_name", ["name"])
            batch.drop_column("org_id")

    with op.batch_alter_table("users", naming_convention=NAMING) as batch:
        batch.drop_constraint("fk_users_org_id_organizations", type_="foreignkey")
        batch.drop_column("org_id")

    op.drop_table("organizations")
