"""backfill workflows.created_by from username to principal_id

Revision ID: 5e806924cfec
Revises: g4h5i6j7k8l9
Create Date: 2026-08-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e806924cfec'
down_revision: Union[str, Sequence[str], None] = 'g4h5i6j7k8l9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Re-key `workflows.created_by` from username to the owner's principal_id.

    `g4h5i6j7k8l9` introduced `workflows.created_by`, populated by the app as
    `user.username` at deploy time. A later change moved the *authorization*
    comparison (My Teams / run-ownership filtering, see `main.py` and
    `db/workflows.py::publish_workflow_version`) to compare against the
    creator's immutable `users.principal_id` instead, since usernames are
    reusable after account deletion. Without this backfill, every workflow
    deployed before that change keeps a username in `created_by`, which no
    longer matches any `principal_id` -- its owner's teams silently vanish
    from `GET /api/workflows`/My Teams and 404 on graph/run (Codex review
    finding). Idempotent: a row already holding a principal_id (set by the
    new code, or a second run of this migration) matches no username and is
    left untouched. Usernames are globally unique (`users.username`), so the
    join needs no org scoping.
    """
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE workflows "
            "SET created_by = ("
            "  SELECT users.principal_id FROM users "
            "  WHERE users.username = workflows.created_by"
            ") "
            "WHERE created_by IN (SELECT username FROM users)"
        )
    )


def downgrade() -> None:
    """No-op: the pre-image (username) isn't recoverable from principal_id alone."""
    pass
