"""add per-org email filter + budget settings and trigger message counter (Phase 4a)

Revision ID: l9m0n1o2p3q4
Revises: k8l9m0n1o2p3
Create Date: 2026-08-17 22:00:00.000000

Purely additive. `skip_bulk` defaults True (bulk mail is filtered by default,
the one deliberate behaviour change this phase makes); both budget caps start
NULL, so no org gains a limit it did not ask for.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'l9m0n1o2p3q4'
down_revision: Union[str, Sequence[str], None] = 'k8l9m0n1o2p3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FILTER = "org_email_filter_settings"
_FILTER_INDEX = "ix_org_email_filter_settings_org_id"
_BUDGET = "org_email_budget_settings"
_BUDGET_INDEX = "ix_org_email_budget_settings_org_id"
_TRIGGERS = "email_triggers"
_MESSAGES_TODAY = "messages_today"


def _tables(inspector) -> set:
    return set(inspector.get_table_names())


def _columns(inspector, table: str) -> set:
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _tables(inspector)

    if _FILTER not in tables:
        op.create_table(
            _FILTER,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"),
                      nullable=False),
            sa.Column("skip_bulk", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sender_blocklist", sa.JSON(), nullable=True),
            sa.Column("sender_allowlist", sa.JSON(), nullable=True),
            sa.Column("subject_blocklist", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index(_FILTER_INDEX, _FILTER, ["org_id"], unique=True)

    if _BUDGET not in tables:
        op.create_table(
            _BUDGET,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"),
                      nullable=False),
            sa.Column("daily_message_cap", sa.Integer(), nullable=True),
            sa.Column("monthly_cost_cap", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index(_BUDGET_INDEX, _BUDGET, ["org_id"], unique=True)

    if _TRIGGERS in tables and _MESSAGES_TODAY not in _columns(inspector, _TRIGGERS):
        op.add_column(
            _TRIGGERS,
            sa.Column(_MESSAGES_TODAY, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    """Downgrade schema.

    SQLite in this project's venv is 3.45.3, past the 3.35 that made
    `ALTER TABLE ... DROP COLUMN` work, so no batch-mode rebuild is needed.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _tables(inspector)

    if _MESSAGES_TODAY in _columns(inspector, _TRIGGERS):
        op.drop_column(_TRIGGERS, _MESSAGES_TODAY)

    if _BUDGET in tables:
        op.drop_index(_BUDGET_INDEX, table_name=_BUDGET)
        op.drop_table(_BUDGET)

    if _FILTER in tables:
        op.drop_index(_FILTER_INDEX, table_name=_FILTER)
        op.drop_table(_FILTER)
