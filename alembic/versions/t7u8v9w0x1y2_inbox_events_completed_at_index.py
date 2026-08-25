"""inbox_events: (org_id, status, completed_at) index for health metrics

Revision ID: t7u8v9w0x1y2
Revises: s6t7u8v9w0x1
Create Date: 2026-08-25 00:00:00.000000

`trigger_metrics.collect` bounds its 24h done/failed window in SQL now; this
index lets that query range-scan completed_at within (org, status) instead of
reading an org's whole terminal history on every `admin check-health` run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 't7u8v9w0x1y2'
down_revision: Union[str, Sequence[str], None] = 's6t7u8v9w0x1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "inbox_events"
_INDEX = "ix_inbox_events_org_id_status_completed_at"


def _index_names(bind) -> set:
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    """Guarded (create_all-at-import idempotency, same as the other
    migrations): a database booted by the backend before `alembic upgrade
    head` already has the index."""
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    if _INDEX in _index_names(bind):
        return
    op.create_index(_INDEX, _TABLE, ["org_id", "status", "completed_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    if _INDEX not in _index_names(bind):
        return
    op.drop_index(_INDEX, table_name=_TABLE)
