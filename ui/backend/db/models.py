"""SQLAlchemy schema for the per-deployment persistence layer.

See docs/team_builder_methodology.md, Phase 1. This defines the tables; the
APIs that read/write most of them (CRUD endpoints, RunRegistry-backed runs,
usage metering) are built incrementally in later phases. `builder_sessions`
is the new concept this phase introduces -- the wizard's session state
machine (intent -> requirements -> spec -> solution -> testing -> deployed).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    """A simple per-deployment login (Phase 3)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AgentRecord(Base):
    """An Agent's `raw` config (the technical fields from `AgentSpec.to_raw()`)."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class TeamRecord(Base):
    """A Team's `raw` config (the technical fields from `TeamSpec.to_raw()`)."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class KnowledgeBaseRecord(Base):
    """A KnowledgeBase's `raw` config (the technical fields from `KnowledgeBaseSpec.to_raw()`)."""

    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class SkillRecord(Base):
    """A Skill's `raw` config (the technical fields from `SkillSpec.to_raw()`)."""

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class WorkflowRecord(Base):
    """A Workflow's `raw` config (agents/teams/knowledge_bases/workflow.steps,
    i.e. `Specification.to_raw()`) plus its lifecycle status."""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    # draft | ready_for_testing | deployed -- mirrors the Solution/Testing/
    # Deployment stages of docs/team_builder_methodology.md.
    status: Mapped[str] = mapped_column(default="draft")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class BuilderSession(Base):
    """State for one customer's trip through the Team Builder Wizard.

    `status` tracks which of the six methodology stages the session is in;
    `requirements_json`/`specification_json` hold the structured outputs of
    the Business Analyst / Solution Architect agents (the latter including
    friendly display fields, per `core/specification.py`); `feedback_history`
    records each round of customer feedback that sent the session back to an
    earlier stage.
    """

    __tablename__ = "builder_sessions"

    # uuid4 hex (truncated), matching RunRegistry's id style (ui/backend/registry.py).
    id: Mapped[str] = mapped_column(primary_key=True)
    intent_text: Mapped[str] = mapped_column(default="")
    as_is_text: Mapped[str] = mapped_column(default="")
    requirements_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    specification_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # intent | requirements | spec | solution | testing | deployed
    status: Mapped[str] = mapped_column(default="intent")
    feedback_history: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class Run(Base):
    """A workflow execution. Persisted replacement for `RunRegistry`'s
    in-memory `Run` dataclass (ui/backend/registry.py), wired up in Phase 5."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(primary_key=True)
    workflow: Mapped[str]
    input: Mapped[str]
    output: Mapped[Optional[str]] = mapped_column(nullable=True)
    # running | completed | failed
    status: Mapped[str] = mapped_column(default="running")
    builder_session_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("builder_sessions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class TraceEventRecord(Base):
    """One `TraceEvent` (core/trace.py) emitted during a `Run`, in order."""

    __tablename__ = "trace_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    seq: Mapped[int]
    type: Mapped[str]
    agent: Mapped[Optional[str]] = mapped_column(nullable=True)
    data: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class UsageRecord(Base):
    """Per-agent token usage for a `Run`, for usage-based metering (Phase 3)."""

    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    agent: Mapped[Optional[str]] = mapped_column(nullable=True)
    model: Mapped[Optional[str]] = mapped_column(nullable=True)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_estimate: Mapped[Optional[float]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class ModelCatalogEntry(Base):
    """Maps a model `spec` string (e.g. "openai:gpt-4o-mini" or "fake:hi") to a
    customer-friendly name, a complexity `tier`, and per-1K-token pricing.

    Used by the Solution Architect when generating a Specification (so it
    picks models by "role complexity" rather than raw provider names) and by
    `usage_records` for cost-estimate conversion (Phase 3)."""

    __tablename__ = "model_catalog"

    id: Mapped[int] = mapped_column(primary_key=True)
    spec: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str]
    description: Mapped[str] = mapped_column(default="")
    # fast | balanced | advanced -- a rough complexity/cost tier.
    tier: Mapped[str] = mapped_column(default="balanced")
    input_price_per_1k: Mapped[float] = mapped_column(default=0.0)
    output_price_per_1k: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
