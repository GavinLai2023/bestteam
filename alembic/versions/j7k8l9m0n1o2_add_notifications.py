"""add notifications, webhook settings and trigger alert state (email Phase 3a)

Revision ID: j7k8l9m0n1o2
Revises: i6j7k8l9m0n1
Create Date: 2026-08-17 18:00:00.000000

Phase 0 made a failing trigger visible; nothing told anyone. This adds the
durable side of alerting: the notifications themselves (which double as the
webhook outbox), where an org wants them delivered, the trigger's alert state,
and the admin-entered Microsoft 365 client-secret expiry date.

Purely additive -- no backfill. Existing triggers start at zero consecutive
faults with no fingerprint, which is the correct "healthy, nothing reported
yet" state.

Guarded ops (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has these tables and
columns when this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'j7k8l9m0n1o2'
down_revision: Union[str, Sequence[str], None] = 'i6j7k8l9m0n1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NOTIFICATIONS = "notifications"
_SETTINGS = "org_notification_settings"
_TRIGGERS = "email_triggers"
_CREDENTIALS = "org_email_credentials"

_TRIGGER_COLUMNS = {
    "consecutive_faults": sa.Column(
        "consecutive_faults", sa.Integer(), nullable=False, server_default="0"
    ),
    "alerted_fingerprint": sa.Column("alerted_fingerprint", sa.String(), nullable=True),
}


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

    if _NOTIFICATIONS not in tables:
        op.create_table(
            _NOTIFICATIONS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("severity", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("body", sa.String(), nullable=False),
            sa.Column("fingerprint", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("read_at", sa.DateTime(), nullable=True),
            sa.Column("delivery_state", sa.String(), nullable=False, server_default="pending"),
            sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("last_delivery_error", sa.String(), nullable=True),
        )
        op.create_index("ix_notifications_org_id", _NOTIFICATIONS, ["org_id"])

    if _SETTINGS not in tables:
        op.create_table(
            _SETTINGS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"),
                      nullable=False, unique=True),
            sa.Column("webhook_url", sa.String(), nullable=True),
            sa.Column("webhook_secret_encrypted", sa.String(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    existing = _columns(inspector, _TRIGGERS)
    if existing:
        for name, column in _TRIGGER_COLUMNS.items():
            if name not in existing:
                op.add_column(_TRIGGERS, column)

    if "oauth_secret_expires_at" not in _columns(inspector, _CREDENTIALS):
        if _CREDENTIALS in tables:
            op.add_column(
                _CREDENTIALS,
                sa.Column("oauth_secret_expires_at", sa.DateTime(), nullable=True),
            )


def downgrade() -> None:
    """Downgrade schema.

    SQLite in this project's venv is 3.45.3, well past the 3.35 that made
    `ALTER TABLE ... DROP COLUMN` work, so no batch-mode table rebuild is
    needed.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _tables(inspector)

    if "oauth_secret_expires_at" in _columns(inspector, _CREDENTIALS):
        op.drop_column(_CREDENTIALS, "oauth_secret_expires_at")

    existing = _columns(inspector, _TRIGGERS)
    for name in reversed(list(_TRIGGER_COLUMNS)):
        if name in existing:
            op.drop_column(_TRIGGERS, name)

    if _SETTINGS in tables:
        op.drop_table(_SETTINGS)
    if _NOTIFICATIONS in tables:
        op.drop_index("ix_notifications_org_id", table_name=_NOTIFICATIONS)
        op.drop_table(_NOTIFICATIONS)
