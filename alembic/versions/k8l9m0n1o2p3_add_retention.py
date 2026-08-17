"""add per-org run retention settings and runs.content_purged_at (email Phase 3b)

Revision ID: k8l9m0n1o2p3
Revises: j7k8l9m0n1o2
Create Date: 2026-08-17 20:00:00.000000

Email-derived model output used to persist forever. This adds the policy row
(`org_retention_settings`) and the stamp a purge leaves behind
(`runs.content_purged_at`).

Purely additive -- no backfill. `run_retention_days` starts NULL, which means
keep forever, so an upgrade deletes nothing (design invariant I5).

Guarded ops (same reason as the other migrations): `ui/backend/db_session.py`
runs `create_all` at import, so a fresh database already has this table and
column when this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'k8l9m0n1o2p3'
down_revision: Union[str, Sequence[str], None] = 'j7k8l9m0n1o2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RETENTION = "org_retention_settings"
_RETENTION_INDEX = "ix_org_retention_settings_org_id"
_RUNS = "runs"
_PURGED_AT = "content_purged_at"


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

    if _RETENTION not in tables:
        op.create_table(
            _RETENTION,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"),
                      nullable=False),
            sa.Column("run_retention_days", sa.Integer(), nullable=True),
            sa.Column("last_swept_at", sa.DateTime(), nullable=True),
            sa.Column("last_purged_count", sa.Integer(), nullable=False, server_default="0"),
        )
        # Unique index rather than a unique constraint: that is what the ORM's
        # `unique=True, index=True` column produces, so both paths match.
        op.create_index(_RETENTION_INDEX, _RETENTION, ["org_id"], unique=True)

    if _RUNS in tables and _PURGED_AT not in _columns(inspector, _RUNS):
        op.add_column(_RUNS, sa.Column(_PURGED_AT, sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    SQLite in this project's venv is 3.45.3, well past the 3.35 that made
    `ALTER TABLE ... DROP COLUMN` work, so no batch-mode table rebuild is
    needed.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _tables(inspector)

    if _PURGED_AT in _columns(inspector, _RUNS):
        op.drop_column(_RUNS, _PURGED_AT)

    if _RETENTION in tables:
        op.drop_index(_RETENTION_INDEX, table_name=_RETENTION)
        op.drop_table(_RETENTION)
