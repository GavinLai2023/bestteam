"""add inbox_events (durable per-message ledger, email automation Phase 1)

Revision ID: h5i6j7k8l9m0
Revises: d2e3f4a5b6c7
Create Date: 2026-08-17 12:00:00.000000

Decouples "this message needs processing" from the run that processes it, so
advancing the mailbox cursor can no longer consume mail that nothing ran.

Guarded op (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has the table when
this migration runs. No backfill -- existing triggers keep their `last_uid`
and record events for whatever arrives above it on the next poll; runs that
are in flight at upgrade time have no rows and fall back to the pre-ledger
retry path in `email_trigger.py::retry_triggered_run`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'h5i6j7k8l9m0'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "inbox_events"):
        return
    op.create_table(
        "inbox_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("connector_type", sa.String(), nullable=False, server_default="imap"),
        sa.Column("mailbox_identity", sa.String(), nullable=False),
        # Never nullable: SQLite treats NULLs as distinct in a UNIQUE
        # constraint, which would silently disable dedup for any connector
        # without a generation concept.
        sa.Column("mailbox_generation", sa.String(), nullable=False, server_default=""),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "org_id", "connector_type", "mailbox_identity",
            "mailbox_generation", "external_id",
            name="uq_inbox_events_identity",
        ),
    )
    op.create_index(
        "ix_inbox_events_org_id_status_id", "inbox_events", ["org_id", "status", "id"]
    )
    op.create_index("ix_inbox_events_run_id", "inbox_events", ["run_id"])


def downgrade() -> None:
    """Downgrade schema (drops the durable ledger)."""
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "inbox_events"):
        op.drop_index("ix_inbox_events_run_id", table_name="inbox_events")
        op.drop_index("ix_inbox_events_org_id_status_id", table_name="inbox_events")
        op.drop_table("inbox_events")
