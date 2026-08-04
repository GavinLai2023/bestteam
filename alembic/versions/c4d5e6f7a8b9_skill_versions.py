"""immutable skill versions and workflow dependency pins

Revision ID: c4d5e6f7a8b9
Revises: b8c9d0e1f2a3
Create Date: 2026-08-01 00:00:00.000000

Guarded/idempotent for the project's create_all-before-Alembic deployment
order. Existing skill configs become v1 and every existing skill dependency
is pinned to that v1, preserving behavior at migration time.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_fk(bind, table: str, column: str, referred_table: str) -> bool:
    return any(
        fk.get("constrained_columns") == [column]
        and fk.get("referred_table") == referred_table
        for fk in sa.inspect(bind).get_foreign_keys(table)
    )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "skill_versions" not in tables:
        op.create_table(
            "skill_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "skill_id",
                sa.Integer(),
                sa.ForeignKey("skills.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "skill_id", "version_number",
                name="uq_skill_versions_skill_id_version_number",
            ),
        )

    if not _has_column(bind, "skills", "current_version_id"):
        with op.batch_alter_table("skills") as batch:
            batch.add_column(sa.Column("current_version_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_skills_current_version_id_skill_versions",
                "skill_versions",
                ["current_version_id"],
                ["id"],
            )
    elif not _has_fk(bind, "skills", "current_version_id", "skill_versions"):
        # Retry-safe repair for an interrupted/older copy of this migration
        # that added the column but not its constraint.
        with op.batch_alter_table("skills") as batch:
            batch.create_foreign_key(
                "fk_skills_current_version_id_skill_versions",
                "skill_versions",
                ["current_version_id"],
                ["id"],
            )
    if not _has_column(bind, "workflow_dependencies", "resource_version_id"):
        with op.batch_alter_table("workflow_dependencies") as batch:
            batch.add_column(sa.Column("resource_version_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_workflow_dependencies_resource_version_id_skill_versions",
                "skill_versions",
                ["resource_version_id"],
                ["id"],
            )
    elif not _has_fk(
        bind,
        "workflow_dependencies",
        "resource_version_id",
        "skill_versions",
    ):
        with op.batch_alter_table("workflow_dependencies") as batch:
            batch.create_foreign_key(
                "fk_workflow_dependencies_resource_version_id_skill_versions",
                "skill_versions",
                ["resource_version_id"],
                ["id"],
            )

    # Row-wise so an interrupted run that inserted v1 but did not move the
    # pointer resumes without duplicating (skill_id, version_number).
    skills = bind.execute(sa.text(
        "SELECT id, config, created_at FROM skills WHERE current_version_id IS NULL"
    )).fetchall()
    for skill_id, config, created_at in skills:
        version_id = bind.execute(sa.text(
            "SELECT id FROM skill_versions "
            "WHERE skill_id = :skill_id ORDER BY version_number DESC LIMIT 1"
        ), {"skill_id": skill_id}).scalar()
        if version_id is None:
            bind.execute(sa.text(
                "INSERT INTO skill_versions "
                "(skill_id, version_number, config, created_by, created_at) "
                "VALUES (:skill_id, 1, :config, NULL, :created_at)"
            ), {"skill_id": skill_id, "config": config, "created_at": created_at})
            version_id = bind.execute(sa.text(
                "SELECT id FROM skill_versions WHERE skill_id = :skill_id "
                "ORDER BY version_number DESC LIMIT 1"
            ), {"skill_id": skill_id}).scalar()
        bind.execute(sa.text(
            "UPDATE skills SET current_version_id = :version_id WHERE id = :skill_id"
        ), {"version_id": version_id, "skill_id": skill_id})

    bind.execute(sa.text(
        "UPDATE workflow_dependencies SET resource_version_id = ("
        "  SELECT skills.current_version_id FROM skills "
        "  WHERE skills.id = workflow_dependencies.resource_id"
        ") WHERE resource_kind = 'skill' AND resource_version_id IS NULL"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "workflow_dependencies", "resource_version_id"):
        with op.batch_alter_table("workflow_dependencies") as batch:
            batch.drop_column("resource_version_id")
    if _has_column(bind, "skills", "current_version_id"):
        with op.batch_alter_table("skills") as batch:
            batch.drop_column("current_version_id")
    if "skill_versions" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("skill_versions")
