"""add knowledge_chunks.page/heading for chunk-level citations (KB beta P0-3)

Revision ID: m0n1o2p3q4r5
Revises: l9m0n1o2p3q4
Create Date: 2026-08-18 10:00:00.000000

Purely additive and nullable. Chunks ingested before this revision keep NULL
for both and cite their filename alone, exactly as they did -- there is no
backfill, because page and heading can only be recovered by re-parsing the
original documents, which a re-upload already does.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'm0n1o2p3q4r5'
down_revision: Union[str, Sequence[str], None] = 'l9m0n1o2p3q4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHUNKS = "knowledge_chunks"
_PAGE = "page"
_HEADING = "heading"


def _columns(inspector, table: str) -> set:
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = _columns(inspector, _CHUNKS)
    if not columns:
        return  # create_all hasn't run and the table isn't here yet

    if _PAGE not in columns:
        op.add_column(_CHUNKS, sa.Column(_PAGE, sa.Integer(), nullable=True))
    if _HEADING not in columns:
        op.add_column(_CHUNKS, sa.Column(_HEADING, sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    SQLite in this project's venv is 3.45.3, past the 3.35 that made
    `ALTER TABLE ... DROP COLUMN` work, so no batch-mode rebuild is needed.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = _columns(inspector, _CHUNKS)

    if _HEADING in columns:
        op.drop_column(_CHUNKS, _HEADING)
    if _PAGE in columns:
        op.drop_column(_CHUNKS, _PAGE)
