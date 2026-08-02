"""automation_item_results table + runs.trigger_context/retry_of_run_id

Revision ID: c1d2e3f4a5b6
Revises: b8c9d0e1f2a3
Create Date: 2026-08-02 00:00:00.000000

Property Maintenance Inbox Release 1A (spec sections 5.3, 11.1). Guarded/
idempotent, same reason as every other migration here: `ui/backend/db_session.py`
runs `create_all` at import before `alembic upgrade head` runs, so a fresh
database already has the table/columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if not _has_column(bind, "runs", "trigger_context"):
        with op.batch_alter_table("runs") as batch:
            batch.add_column(sa.Column("trigger_context", sa.JSON(), nullable=True))
    if not _has_column(bind, "runs", "retry_of_run_id"):
        with op.batch_alter_table("runs") as batch:
            batch.add_column(sa.Column("retry_of_run_id", sa.String(), nullable=True))

    if "automation_item_results" not in tables:
        op.create_table(
            "automation_item_results",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("source_type", sa.String(), nullable=False, server_default="email"),
            sa.Column("source_key", sa.String(), nullable=False),
            sa.Column("result_type", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("needs_attention", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "run_id", "source_key", name="uq_automation_item_results_run_id_source_key"
            ),
        )
        op.create_index(
            "ix_automation_item_results_org_id_created_at",
            "automation_item_results", ["org_id", "created_at"],
        )
        op.create_index(
            "ix_automation_item_results_org_id_needs_attention_created_at",
            "automation_item_results", ["org_id", "needs_attention", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "automation_item_results" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("automation_item_results")
    if _has_column(bind, "runs", "retry_of_run_id"):
        with op.batch_alter_table("runs") as batch:
            batch.drop_column("retry_of_run_id")
    if _has_column(bind, "runs", "trigger_context"):
        with op.batch_alter_table("runs") as batch:
            batch.drop_column("trigger_context")
