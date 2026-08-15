"""add share_links.turns_today/turns_date (link-level aggregate daily cap)

Revision ID: b5c1d8e3f2a9
Revises: a3f7c9d2e6b1
Create Date: 2026-08-15 09:00:00.000000

The per-ShareSession daily cap alone is not a real cost control: a visitor
that simply never stores the session cookie gets a brand-new, free
ShareSession (and so a fresh allowance) on every request. These two columns
give `ShareLink` the same `turns_today`/`turns_date` CAS shape
`ShareSession` already has, so `daily_cap` is also the aggregate ceiling
across everyone using one link, per day (final whole-branch review C1).

Guarded ops (same reason as every other migration here):
`ui/backend/db_session.py` runs `create_all` at import, so a fresh database
already has these columns when this migration runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5c1d8e3f2a9'
down_revision: Union[str, Sequence[str], None] = 'a3f7c9d2e6b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, "share_links", "turns_today"):
        op.add_column(
            "share_links",
            sa.Column("turns_today", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column(inspector, "share_links", "turns_date"):
        op.add_column("share_links", sa.Column("turns_date", sa.String(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "share_links", "turns_date"):
        op.drop_column("share_links", "turns_date")
    if _has_column(inspector, "share_links", "turns_today"):
        op.drop_column("share_links", "turns_today")
