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

from sqlalchemy import JSON, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    """A customer organisation on a shared multi-org deployment.

    Org-owned resources carry an `org_id`; users belong to exactly one org
    except platform operators (`users.org_id IS NULL`). A single-customer
    deployment simply has one org (the migration backfills `default`) -- the
    same code serves both deployment models. See
    docs/superpowers/specs/2026-07-15-org-multi-tenancy-design.md.
    """

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class User(Base):
    """A login. Belongs to an organization, or is a platform operator (org NULL)."""

    __tablename__ = "users"
    # One member per org is a hard invariant, not just an app-level check: an
    # org's resources (notably the shared mailbox) have no per-member privilege
    # separation yet. The partial unique index enforces it in the schema so a
    # race or a bypass of `create_user` still can't create a second member,
    # while leaving platform operators (org_id NULL, excluded from the index)
    # free to be many. See docs/DECISIONS.md ("one member per org").
    __table_args__ = (
        Index(
            "uq_users_org_id_not_null",
            "org_id",
            unique=True,
            sqlite_where=text("org_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Usernames stay globally unique across orgs: the JWT `sub` claim and
    # per-user memory both key on the bare username.
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    # Admins can reach the Advanced config page and the memory-management UI
    # (see auth_api.get_current_admin). Granted only via the `ui.backend.admin`
    # operator CLI -- never from registration or an env username match.
    is_admin: Mapped[bool] = mapped_column(default=False)
    # NULL = platform operator (not part of any customer org).
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AgentRecord(Base):
    """An Agent's `raw` config (the technical fields from `AgentSpec.to_raw()`)."""

    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_agents_org_id_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class TeamRecord(Base):
    """A Team's `raw` config (the technical fields from `TeamSpec.to_raw()`)."""

    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_teams_org_id_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class KnowledgeBaseRecord(Base):
    """A KnowledgeBase's `raw` config (the technical fields from `KnowledgeBaseSpec.to_raw()`)."""

    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_knowledge_bases_org_id_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class SkillRecord(Base):
    """A Skill's `raw` config (the technical fields from `SkillSpec.to_raw()`)."""

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_skills_org_id_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    # NULL = platform built-in skill, visible to every org (e.g. the seeded
    # email_triage_reply); non-null = that org's own skill, which shadows a
    # same-named built-in for that org.
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class WorkflowRecord(Base):
    """A Workflow's `raw` config (agents/teams/knowledge_bases/workflow.steps,
    i.e. `Specification.to_raw()`) plus its lifecycle status."""

    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_workflows_org_id_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    # draft | ready_for_testing | deployed -- mirrors the Solution/Testing/
    # Deployment stages of docs/team_builder_methodology.md.
    status: Mapped[str] = mapped_column(default="draft")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class OrgEmailCredential(Base):
    """One org's mailbox connection for the email tools (per-org secrets store).

    Replaces the process-wide `BESTTEAM_EMAIL_*` env vars on a multi-org
    deployment: each org connects its own mailbox, and a run resolves the
    running org's credentials (see `ui/backend/email_tools.py`). The password
    is encrypted at rest (`secret_store`); `password_encrypted` holds the
    Fernet token, never plaintext. One mailbox per org (unique `org_id`).
    IMAP only for now (`backend='imap'`); Graph/OAuth are future work.
    """

    __tablename__ = "org_email_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, nullable=False
    )
    backend: Mapped[str] = mapped_column(default="imap")
    host: Mapped[str]
    port: Mapped[int] = mapped_column(default=993)
    username: Mapped[str]
    password_encrypted: Mapped[str]
    drafts_folder: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class EmailTrigger(Base):
    """One org's autonomous new-mail trigger (opt-in) plus poller state.

    At most one auto-running team per org (unique `org_id`), mirroring
    one-mailbox-per-org. `last_uid`/`uidvalidity` are the dedup baseline --
    UIDs, never UNSEEN, because the draft-only toolkit deliberately never
    marks mail seen. `runs_today`/`runs_date` implement the daily run cap.
    See docs/superpowers/specs/2026-07-19-email-trigger-autonomous-runs-design.md.
    """

    __tablename__ = "email_triggers"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, nullable=False
    )
    workflow_name: Mapped[str]
    enabled: Mapped[bool] = mapped_column(default=False)
    # Dedup baseline: only UIDs above this trigger a run. Set to the mailbox's
    # current max UID at enable time so the existing backlog never triggers.
    last_uid: Mapped[int] = mapped_column(default=0)
    uidvalidity: Mapped[Optional[int]] = mapped_column(nullable=True)
    # Daily cap state; runs_date is an ISO date string (UTC).
    runs_today: Mapped[int] = mapped_column(default=0)
    runs_date: Mapped[Optional[str]] = mapped_column(nullable=True)
    # Overlap guard: skip a cycle while this run is still `running`.
    last_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(nullable=True)
    # "mailbox" (connectivity/credentials -- auto-clears on the next
    # successful check) | "workflow" (dispatch/build fault -- persists until
    # a real successful dispatch, F5) | None (no error, or a pre-migration
    # row whose kind is unknown -- treated conservatively as sticky).
    last_error_kind: Mapped[Optional[str]] = mapped_column(nullable=True)
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
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    feedback_history: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class Run(Base):
    """A workflow execution. `ui/backend/runtime.py::run_in_background` persists
    one row per run (status/output updated at the terminal event) so usage/trace
    foreign keys reference a real run (CR-012); `RunRegistry` remains the live
    in-memory layer and full restart-survival is deferred to Phase 5."""

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
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # Who started the run (CR-032) -- informational for audit; ownership is
    # org-level via org_id. NULL for legacy/pre-fix runs.
    username: Mapped[Optional[str]] = mapped_column(nullable=True)
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
    # Denormalized from the run's org for per-customer aggregation.
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
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
