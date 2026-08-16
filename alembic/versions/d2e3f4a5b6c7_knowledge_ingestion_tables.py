"""knowledge_ingestion_jobs, knowledge_documents, knowledge_chunks tables

Revision ID: d2e3f4a5b6c7
Revises: b5c1d8e3f2a9
Create Date: 2026-08-16 00:00:00.000000

Guarded/idempotent, same reason as every other migration here:
ui/backend/db_session.py runs create_all at import before
`alembic upgrade head` runs, so a fresh database already has these tables.
See docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "b5c1d8e3f2a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "knowledge_ingestion_jobs" not in tables:
        op.create_table(
            "knowledge_ingestion_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kb_id", sa.Integer(), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
            sa.Column("version", sa.String(), nullable=False),
            # The KB shape this job's chunks were ingested under -- retrieval
            # reads these instead of knowledge_bases.config, which is already
            # advanced to the new spec while the previous generation is still
            # the live one. See ui/backend/db/models.py::IngestionJob.
            sa.Column(
                "kb_type", sa.String(), nullable=False, server_default="local_folder",
            ),
            sa.Column("embedding_model", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("documents_succeeded", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("documents_failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ('queued', 'running', 'completed', 'failed')",
                name="ck_knowledge_ingestion_jobs_status",
            ),
        )
        op.create_index(
            "ix_knowledge_ingestion_jobs_kb_id_status_completed_at",
            "knowledge_ingestion_jobs", ["kb_id", "status", "completed_at"],
        )
    else:
        # kb_type/embedding_model were added to this same (never-released)
        # revision after it had already created the table on some developer
        # databases -- add them if that's the shape we find.
        columns = {c["name"] for c in sa.inspect(bind).get_columns("knowledge_ingestion_jobs")}
        if "kb_type" not in columns:
            op.add_column(
                "knowledge_ingestion_jobs",
                sa.Column("kb_type", sa.String(), nullable=False, server_default="local_folder"),
            )
        if "embedding_model" not in columns:
            op.add_column(
                "knowledge_ingestion_jobs",
                sa.Column("embedding_model", sa.String(), nullable=True),
            )

    if "knowledge_documents" not in tables:
        op.create_table(
            "knowledge_documents",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kb_id", sa.Integer(), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column(
                "ingestion_job_id", sa.Integer(),
                sa.ForeignKey("knowledge_ingestion_jobs.id"), nullable=False,
            ),
            sa.Column("filename", sa.String(), nullable=False),
            sa.Column("content_hash", sa.String(), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'parsing', 'chunked', 'failed')",
                name="ck_knowledge_documents_status",
            ),
        )
        op.create_index(
            "ix_knowledge_documents_ingestion_job_id",
            "knowledge_documents", ["ingestion_job_id"],
        )

    if "knowledge_chunks" not in tables:
        op.create_table(
            "knowledge_chunks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "document_id", sa.Integer(),
                sa.ForeignKey("knowledge_documents.id"), nullable=False,
            ),
            sa.Column("kb_id", sa.Integer(), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("text", sa.String(), nullable=False),
            sa.Column("embedding_json", sa.String(), nullable=True),
            sa.Column("embedding_model", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_knowledge_chunks_document_id_chunk_index",
            "knowledge_chunks", ["document_id", "chunk_index"],
        )
        op.create_index("ix_knowledge_chunks_kb_id", "knowledge_chunks", ["kb_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "knowledge_chunks" in tables:
        op.drop_table("knowledge_chunks")
    if "knowledge_documents" in tables:
        op.drop_table("knowledge_documents")
    if "knowledge_ingestion_jobs" in tables:
        op.drop_table("knowledge_ingestion_jobs")
