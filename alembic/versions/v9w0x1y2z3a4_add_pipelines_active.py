"""pipelines: an `active` flag, the customer's reversible pause

Revision ID: v9w0x1y2z3a4
Revises: u8v9w0x1y2z3
Create Date: 2026-08-30 00:00:00.000000

A deployed team had no off switch below the org level: `organizations.active`
suspends the whole customer, and deleting a live team is still deferred. This
is the reversible half -- False stops runs from every entry point while the
config, its versions and all history stay exactly where they are.

Deliberately not a fourth `pipelines.status` value: a paused team is still
deployed, and every reader of `status` treats "deployed" as "has a published
version", which stays true.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v9w0x1y2z3a4'
down_revision: Union[str, Sequence[str], None] = 'u8v9w0x1y2z3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "pipelines"
_COLUMN = "active"


def _columns(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    """Guarded (create_all-at-import idempotency, same as the other
    migrations): a database booted by the backend before `alembic upgrade
    head` already has the column.

    `server_default="1"` backfills every existing row to active, so an upgrade
    pauses nothing.
    """
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    if _COLUMN in _columns(bind):
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    if _COLUMN not in _columns(bind):
        return
    op.drop_column(_TABLE, _COLUMN)
