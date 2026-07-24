"""workflow_versions table + current/version pointers + backfill v1

Revision ID: c3f5a1b8e2d4
Revises: b1d7e4f2a9c8
Create Date: 2026-07-25 00:00:00.000000

Introduces immutable workflow versions (P1-01/02/03). Guarded/idempotent:
db_session runs create_all at import before upgrade, so a fresh DB already has
the table/columns -- create/add only when absent. Backfill gives every existing
workflow exactly one v1 (config copied verbatim) and sets the pointer; the
`current_version_id IS NULL` filter makes a re-run a no-op. No JSON-parsing
backfill for builder_sessions.workflow_id / runs.workflow_version_id -- those
forward-populate on the next deploy / run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f5a1b8e2d4"
down_revision: Union[str, Sequence[str], None] = "b1d7e4f2a9c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "workflow_versions" not in tables:
        op.create_table(
            "workflow_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workflow_id", sa.Integer(), sa.ForeignKey("workflows.id"), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "workflow_id", "version_number",
                name="uq_workflow_versions_workflow_id_version_number",
            ),
        )

    if not _has_column(bind, "workflows", "current_version_id"):
        with op.batch_alter_table("workflows") as batch:
            batch.add_column(sa.Column("current_version_id", sa.Integer(), nullable=True))
    if not _has_column(bind, "builder_sessions", "workflow_id"):
        with op.batch_alter_table("builder_sessions") as batch:
            batch.add_column(sa.Column("workflow_id", sa.Integer(), nullable=True))
    if not _has_column(bind, "runs", "workflow_version_id"):
        with op.batch_alter_table("runs") as batch:
            batch.add_column(sa.Column("workflow_version_id", sa.Integer(), nullable=True))

    # Backfill: one v1 per workflow lacking a pointer, then set the pointer.
    op.execute(
        "INSERT INTO workflow_versions (workflow_id, version_number, config, created_by, created_at) "
        "SELECT id, 1, config, NULL, created_at FROM workflows WHERE current_version_id IS NULL"
    )
    op.execute(
        "UPDATE workflows SET current_version_id = ("
        "  SELECT wv.id FROM workflow_versions wv "
        "  WHERE wv.workflow_id = workflows.id AND wv.version_number = 1"
        ") WHERE current_version_id IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "runs", "workflow_version_id"):
        with op.batch_alter_table("runs") as batch:
            batch.drop_column("workflow_version_id")
    if _has_column(bind, "builder_sessions", "workflow_id"):
        with op.batch_alter_table("builder_sessions") as batch:
            batch.drop_column("workflow_id")
    if _has_column(bind, "workflows", "current_version_id"):
        with op.batch_alter_table("workflows") as batch:
            batch.drop_column("current_version_id")
    if "workflow_versions" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("workflow_versions")
