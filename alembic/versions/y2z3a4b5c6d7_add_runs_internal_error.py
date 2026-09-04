"""runs: an operator-only copy of why the run really failed

Revision ID: y2z3a4b5c6d7
Revises: x1y2z3a4b5c6
Create Date: 2026-09-04 00:00:00.000000

`runs.output` is what the customer reads, so a failure's text there is
sanitized: a provider's own exception can name the model, the provider and the
account's billing state, and the Activity page renders it outside the "show
technical" fold. That left an operator with nothing but the container log, so
the real text now lands here instead of being dropped.

Nullable with no backfill: runs that failed before this migration keep the only
copy they ever had, in the log.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'y2z3a4b5c6d7'
down_revision: Union[str, Sequence[str], None] = 'x1y2z3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "runs"
_COLUMN = "internal_error"


def _columns(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    """Guarded (create_all-at-import idempotency, same as the other
    migrations): a database booted by the backend before `alembic upgrade
    head` already has the column."""
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    if _COLUMN in _columns(bind):
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    if _COLUMN not in _columns(bind):
        return
    op.drop_column(_TABLE, _COLUMN)
