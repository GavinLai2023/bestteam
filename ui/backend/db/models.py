"""SQLAlchemy schema for the per-deployment persistence layer.

See docs/team_builder_methodology.md, Phase 1. This defines the tables; the
APIs that read/write most of them (CRUD endpoints, RunRegistry-backed runs,
usage metering) are built incrementally in later phases. `builder_sessions`
is the new concept this phase introduces -- the wizard's session state
machine (intent -> requirements -> spec -> solution -> testing -> deployed).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """Format a `created_at`/`updated_at` column for API responses.

    Every such column is populated via `_utcnow()`, but SQLite round-trips it
    tzinfo-naive -- `dt.isoformat()` alone then omits the UTC marker, and a
    frontend `Date` parses that as local time instead (the actual value is
    always UTC, so `.replace` is correct, not a guess)."""
    return dt.replace(tzinfo=timezone.utc).isoformat()


def new_security_stamp() -> str:
    """A fresh random per-account credential generation (see User.security_stamp)."""
    return secrets.token_hex(16)


def new_principal_id() -> str:
    """A fresh random immutable per-account principal (see User.principal_id)."""
    return secrets.token_hex(16)


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
    # False = deactivated (full suspend): the org's member can't log in and
    # every org-scoped surface 403s, but all data is kept and it's reversible.
    active: Mapped[bool] = mapped_column(default=True, server_default=text("1"))


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
    # Random per-account credential generation embedded in every access token
    # and WS ticket and verified on use. Regenerated on password reset (revokes
    # existing sessions) and fresh at creation -- so a recreated username (new
    # random stamp) can't be reached by the deleted account's old credentials
    # (review r-ext2 #1/#3). An immutable random value, not a timestamp, so
    # there's no ordering/resolution race.
    security_stamp: Mapped[Optional[str]] = mapped_column(default=new_security_stamp, nullable=True)
    # Immutable per-account principal for the memory deletion-lifecycle. Unlike
    # security_stamp it is NEVER rotated (not on password reset), so it's a stable
    # memory principal: a run's recall/writes are scoped to it, and a deleted-then-
    # recreated username gets a fresh value it can't reach the old account's memory
    # with. Random (no ordering race), set once at creation. nullable=True tolerates
    # a pre-migration row until the backfill runs.
    principal_id: Mapped[Optional[str]] = mapped_column(default=new_principal_id, nullable=True)


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


class IngestionJob(Base):
    """One async ingestion run for an upload-managed KnowledgeBaseRecord.

    A KB's live document set is always its most recent `completed` job's
    KnowledgeDocument/KnowledgeChunk rows -- the `status="completed"` flip
    is the atomic swap (no CURRENT-pointer file needed for this path,
    unlike the legacy file-based read path). A `queued`/`running`/`failed`
    job is invisible to retrieval. See
    docs/superpowers/specs/2026-08-16-kb-document-chunk-ingestion-design.md.
    """

    __tablename__ = "knowledge_ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_knowledge_ingestion_jobs_status",
        ),
        Index(
            "ix_knowledge_ingestion_jobs_kb_id_status_completed_at",
            "kb_id", "status", "completed_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), nullable=False)
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # The same `v_<hex>` identifier used for the on-disk version directory
    # (see ui/backend/knowledge_bases.py) -- traceable job <-> directory
    # correspondence.
    version: Mapped[str]
    # The KB shape this job's chunks were actually ingested under. Retrieval
    # reads these -- NOT the KnowledgeBaseRecord's `config` -- to decide which
    # KnowledgeBase subclass to rebuild and with which query-time embedding
    # model: `config` is advanced to the NEW spec the moment an upload is
    # dispatched, while the live document set stays the previous completed
    # job's chunks until the new job finishes (and forever, if it fails). Only
    # the job knows whether its own chunks carry embeddings, and from which
    # model.
    kb_type: Mapped[str] = mapped_column(default="local_folder")
    embedding_model: Mapped[Optional[str]] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(default="queued")
    file_count: Mapped[int] = mapped_column(default=0)
    documents_succeeded: Mapped[int] = mapped_column(default=0)
    documents_failed: Mapped[int] = mapped_column(default=0)
    error: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class KnowledgeDocument(Base):
    """One uploaded file's ingestion outcome within an IngestionJob.

    Per-document status means one bad file in a batch doesn't fail the
    whole job -- see IngestionJob's docstring.
    """

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'parsing', 'chunked', 'failed')",
            name="ck_knowledge_documents_status",
        ),
        Index("ix_knowledge_documents_ingestion_job_id", "ingestion_job_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), nullable=False)
    ingestion_job_id: Mapped[int] = mapped_column(ForeignKey("knowledge_ingestion_jobs.id"), nullable=False)
    filename: Mapped[str]
    content_hash: Mapped[str]
    size_bytes: Mapped[int]
    status: Mapped[str] = mapped_column(default="pending")
    error: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class KnowledgeChunk(Base):
    """One chunk of a KnowledgeDocument's parsed text.

    `embedding_json` is a JSON-encoded `List[float]` (same TEXT-column
    shape as `memories.embedding_json` in core/memory.py), populated only
    for `vector`/`hybrid` KBs.

    `page`/`heading` are where in the document the chunk came from, and are
    what a retrieval result cites beyond the filename. Both are nullable
    because only some formats supply them: `page` for a PDF (chunked per
    page, so it is exact), `heading` for Markdown (the section the chunk
    opens under, approximate). Rows ingested before these columns existed
    have NULL for both and cite their filename alone, as they always did.
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("ix_knowledge_chunks_document_id_chunk_index", "document_id", "chunk_index"),
        Index("ix_knowledge_chunks_kb_id", "kb_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), nullable=False)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), nullable=False)
    chunk_index: Mapped[int]
    text: Mapped[str]
    page: Mapped[Optional[int]] = mapped_column(nullable=True)
    heading: Mapped[Optional[str]] = mapped_column(nullable=True)
    embedding_json: Mapped[Optional[str]] = mapped_column(nullable=True)
    embedding_model: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class SkillRecord(Base):
    """Stable head for a versioned Skill.

    ``config`` mirrors the current SkillVersion for compatibility with existing
    admin/library readers. Deployments never execute this mutable mirror: each
    WorkflowDependency pins an immutable SkillVersion instead.
    """

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
    current_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("skill_versions.id"), nullable=True
    )


class SkillVersion(Base):
    """An immutable snapshot appended whenever a SkillRecord is saved."""

    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint(
            "skill_id", "version_number",
            name="uq_skill_versions_skill_id_version_number",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable intentionally: deleting an unused library head must not destroy
    # version snapshots retained by superseded workflow versions/audit history.
    skill_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"), nullable=True
    )
    version_number: Mapped[int]
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_by: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class WorkflowRecord(Base):
    """A Workflow's `raw` config (agents/teams/knowledge_bases/workflow.steps,
    i.e. `Specification.to_raw()`) plus its lifecycle status."""

    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_workflows_org_id_name"),
        CheckConstraint(
            "status IN ('draft', 'ready_for_testing', 'deployed')",
            name="ck_workflows_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # The creator's immutable User.principal_id (NOT username -- usernames are
    # reusable after account deletion, and a username-keyed value here would
    # let a newly created same-named account see/run the deleted account's
    # personal workflows). NULL = admin-shared template, visible to every org
    # member. See db/workflows.py::publish_workflow_version.
    created_by: Mapped[Optional[str]] = mapped_column(nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    # draft | ready_for_testing | deployed -- mirrors the Solution/Testing/
    # Deployment stages of docs/team_builder_methodology.md.
    status: Mapped[str] = mapped_column(default="draft")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
    current_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflow_versions.id"), nullable=True
    )


class WorkflowVersion(Base):
    """An immutable published snapshot of a WorkflowRecord's config.

    Deploy appends one row (never updates an existing one) and points the
    parent WorkflowRecord.current_version_id at it; a Run references the exact
    version it executed. The inline config and referenced SkillVersions are
    frozen; standalone KBs/models are still resolved by name at load."""

    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "version_number",
            name="uq_workflow_versions_workflow_id_version_number",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"))
    version_number: Mapped[int]
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_by: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class WorkflowDependency(Base):
    """A typed record of one skill/KB a published workflow version depends on.

    Materialized at deploy from the version's inline config (agents[*].skills and
    the standalone KBs named in agents[*].tools) so the DB can answer "what depends
    on this resource?" and the skill/KB delete guard can RESTRICT by a precise
    resource_id instead of re-scanning every deployed workflow's JSON (P1-04).
    Written once per version (a version is immutable); resource_id is the resolved
    SkillRecord/KnowledgeBaseRecord id and resource_version_id freezes SkillVersion
    content (NULL only for legacy/unresolved references)."""

    __tablename__ = "workflow_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "workflow_version_id", "resource_kind", "resource_name",
            name="uq_workflow_dependencies_version_kind_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_version_id: Mapped[int] = mapped_column(ForeignKey("workflow_versions.id"))
    resource_kind: Mapped[str]
    resource_name: Mapped[str]
    resource_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    # Populated for resource_kind="skill". The stable skill head id above is
    # retained for reverse dependency/delete queries; this id freezes content.
    resource_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("skill_versions.id"), nullable=True
    )


class OrgEmailCredential(Base):
    """One org's mailbox connection for the email tools (per-org secrets store).

    Replaces the process-wide `BESTTEAM_EMAIL_*` env vars on a multi-org
    deployment: each org connects its own mailbox, and a run resolves the
    running org's credentials (see `ui/backend/email_tools.py`). The password
    is encrypted at rest (`secret_store`); `password_encrypted` holds the
    Fernet token, never plaintext. One mailbox per org (unique `org_id`).

    Always IMAP (`backend='imap'`); `auth_type` selects how it authenticates.
    `password_encrypted` holds the mailbox password for `auth_type='password'`
    and the Entra **client secret** for `auth_type='microsoft_oauth'` -- one
    encrypted column either way, so there is exactly one place a secret is
    written and one place `ensure_secrets_key_for_stored_credentials` checks.
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
    # How this mailbox authenticates: "password" (mailbox / app password) or
    # "microsoft_oauth" (Entra app-only client credentials, SASL XOAUTH2 over
    # IMAP -- Exchange Online no longer accepts basic auth).
    auth_type: Mapped[str] = mapped_column(default="password")
    # Entra identifiers. Not secrets -- the client *secret* is what goes in
    # `password_encrypted`. NULL for password auth.
    oauth_tenant_id: Mapped[Optional[str]] = mapped_column(nullable=True)
    oauth_client_id: Mapped[Optional[str]] = mapped_column(nullable=True)
    # Admin-entered, Microsoft 365 only, optional. Entra client secrets expire
    # in at most two years and the resulting failure looks exactly like a wrong
    # password, so this powers an advance warning. Deliberately NOT read from
    # Graph: that needs `Application.Read.All`, a directory-wide read over
    # every app registration in the customer's tenant, which is far broader
    # than the single-mailbox `IMAP.AccessAsApp` the connection itself uses.
    oauth_secret_expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
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
    # Messages (not runs) handed to a model today, for the per-org message cap.
    # Shares `runs_date` on purpose: one rollover check resets both, so the two
    # counters can never disagree about which day it is.
    messages_today: Mapped[int] = mapped_column(default=0)
    # Overlap guard: skip a cycle while this run is still `running`.
    last_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(nullable=True)
    # "mailbox" (connectivity/credentials -- auto-clears on the next
    # successful check) | "workflow" (dispatch/build fault -- persists until
    # a real successful dispatch, F5) | None (no error, or a pre-migration
    # row whose kind is unknown -- treated conservatively as sticky).
    last_error_kind: Mapped[Optional[str]] = mapped_column(nullable=True)
    # Alerting state (Phase 3a). `consecutive_faults` counts fault cycles of
    # any kind and resets on any healthy outcome; `alerted_fingerprint` is the
    # problem most recently ALERTED about, so a condition already reported
    # stays quiet until it clears. See `ui/backend/trigger_health.py`.
    consecutive_faults: Mapped[int] = mapped_column(default=0)
    alerted_fingerprint: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class InboxEvent(Base):
    """One detected inbound message, durable and independent of any run.

    The row exists so that the commit which consumes the mail (advancing
    `EmailTrigger.last_uid`) is the SAME commit that records the work. Before
    this, `_start_triggered_run` advanced the cursor and only then handed the
    workflow to a thread pool, so a process killed in between consumed mail
    that nothing ever processed.

    Identity is `(org, connector, mailbox, generation, external_id)`. The
    generation is load-bearing: an IMAP UID is only meaningful within a
    UIDVALIDITY, so after a mailbox rebuild UID 7 is a different message and
    must not be mistaken for a duplicate. It is `""` -- never NULL -- for
    connectors with no such concept, because SQLite treats NULLs as distinct
    in a UNIQUE constraint, which would silently disable dedup.

    `connector_type`/`mailbox_generation`/`external_id` are deliberately
    connector-neutral: Phase 2 adds Graph/Gmail and this table will hold real
    customer rows by then. `decision` and the `filtered` status are Phase 4's
    pre-LLM filter outcome: `record_events`'s `decisions` argument inserts a
    row `filtered` with the reason recorded in `decision` instead of
    `pending`, and `release_filtered_event` hands one back for normal
    processing.
    See docs/superpowers/specs/2026-08-17-email-phase-1-inbox-events-design.md.
    """

    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "connector_type", "mailbox_identity",
            "mailbox_generation", "external_id",
            name="uq_inbox_events_identity",
        ),
        Index("ix_inbox_events_org_id_status_id", "org_id", "status", "id"),
        Index("ix_inbox_events_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    connector_type: Mapped[str] = mapped_column(default="imap")
    mailbox_identity: Mapped[str]
    mailbox_generation: Mapped[str] = mapped_column(default="")
    external_id: Mapped[str]
    # pending | claimed | done | failed | filtered
    status: Mapped[str] = mapped_column(default="pending")
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    # Charged when a run is actually dispatched, never at claim -- see
    # db/inbox_events.py::mark_dispatched.
    attempts: Mapped[int] = mapped_column(default=0)
    decision: Mapped[Optional[str]] = mapped_column(nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(nullable=True)
    detected_at: Mapped[datetime] = mapped_column(default=_utcnow)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


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
    # The stable WorkflowRecord (team head) this session deploys to. Set on
    # first deploy; a redeploy publishes a new version under the same head, so
    # two sessions that deploy the same name converge on one head (P1-02).
    workflow_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflows.id"), nullable=True
    )
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
    # The exact immutable version this run executed (P1-03/P1-15). NULL for
    # sandbox test runs (they run the session spec, not a published version)
    # and for pre-migration rows.
    workflow_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflow_versions.id"), nullable=True
    )
    # Server-generated context for an autonomous email-triggered run: mailbox
    # credential id, IMAP UIDVALIDITY, the detected UID batch, folder, and
    # trigger time (see email_trigger.py::_start_triggered_run). NULL for a
    # manual/builder-sandbox run. Never trust a model's own claim about which
    # UIDs it processed -- this is the server's own record of the batch, used
    # both to validate the model's output (automation_results.py) and to
    # revalidate eligibility for POST /api/runs/{id}/retry. See
    # docs/superpowers/specs/2026-08-02-property-maintenance-inbox-phase-1-development-plan.md
    # section 11.1.
    trigger_context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # The run this run retried, if any -- lets the UI show a retry chain and
    # keeps history immutable (a retry always creates a new row).
    retry_of_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    # Phase 3b: when this run's content (input/output/trace/item payloads) was
    # cleared by a retention purge. The row itself survives -- usage_records
    # hangs off it and carries the org's cost history. NULL = never purged.
    content_purged_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AutomationItemResult(Base):
    """One immutable, structured outcome for one input item of one Run.

    Deliberately NOT a `Case`/`WorkOrder`/state machine (spec section 5.1):
    this row describes what a Run did to one input, not an ongoing business
    entity -- there is no status transition, owner, or close action. `payload`
    holds only the minimal extracted/decision fields the vertical needs (never
    a raw email body); `source_key` is always server-generated (see
    `automation_results.py`), never taken from the model's own output, so a
    model can't fabricate or spoof which input it claims to have processed.
    See docs/superpowers/specs/2026-08-02-property-maintenance-inbox-phase-1-development-plan.md
    section 5.3.
    """

    __tablename__ = "automation_item_results"
    __table_args__ = (
        UniqueConstraint("run_id", "source_key", name="uq_automation_item_results_run_id_source_key"),
        Index("ix_automation_item_results_org_id_created_at", "org_id", "created_at"),
        Index(
            "ix_automation_item_results_org_id_needs_attention_created_at",
            "org_id", "needs_attention", "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    # Fixed to "email" in Release 1A (spec section 5.3) -- kept as a column
    # rather than hardcoded so a future non-email automation source doesn't
    # need a schema change.
    source_type: Mapped[str] = mapped_column(default="email")
    source_key: Mapped[str]
    # Fixed to "property_maintenance_email" in Release 1A.
    result_type: Mapped[str]
    # processed | needs_attention | skipped | error
    status: Mapped[str]
    needs_attention: Mapped[bool] = mapped_column(default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
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
    """One metered LLM/embedding call, for usage-based metering (Phase 3).

    One ledger for every kind of spend an org can incur, which is what lets
    `db/email_budget_settings.py`'s monthly cap be a single `SUM` over
    `org_id`. Most rows belong to a `Run`, but a knowledge base's *ingestion*
    embedding spend belongs to an upload rather than a run, so exactly one of
    `run_id`/`ingestion_job_id` is set (`agent="kb:ingest"` for the latter).
    A KB's *query-time* spend is a normal run row -- it rides the calling
    agent's `agent_completed.usage` (see `core/tool_context.py`).
    """

    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    # Set instead of `run_id` for knowledge-base ingestion spend (migration
    # `n1o2p3q4r5s6`), so a row can be traced back to the upload that caused
    # it. The id can outlive its job -- generation pruning and KB deletion
    # both delete `knowledge_ingestion_jobs` rows, and this row deliberately
    # survives them (same "keep the accounting" rule retention follows), so
    # treat it as a provenance label, not a joinable key.
    ingestion_job_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("knowledge_ingestion_jobs.id"), nullable=True
    )
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


class ShareLink(Base):
    """A shareable, revocable entry point letting anonymous colleagues chat
    with one deployed team without a real account.

    Visitor identity is a per-browser ShareSession, never a `users` row --
    this exists specifically so sharing a team doesn't require lifting the
    one-member-per-org constraint (docs/DECISIONS.md). See
    docs/superpowers/specs/2026-08-14-team-sharing-continuous-chat-design.md.
    """

    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"), nullable=False)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    token: Mapped[str] = mapped_column(unique=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    active: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    daily_cap: Mapped[int] = mapped_column(default=30)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    # `daily_cap` is also the AGGREGATE ceiling across every session using
    # this link, per day -- the per-session counter alone is not a real cost
    # control, since a cookie-less client gets a fresh ShareSession (and so a
    # fresh allowance) on every request. Same CAS shape as
    # `ShareSession.turns_today`/`turns_date` (db/share_links.py::
    # try_consume_link_turn).
    turns_today: Mapped[int] = mapped_column(default=0)
    turns_date: Mapped[Optional[str]] = mapped_column(nullable=True)


class ShareSession(Base):
    """One anonymous visitor's browser against one ShareLink.

    Cookie-identified (`session_token`, embedded in a signed cookie by
    `ui/backend/share_auth.py`) -- never cross-visible to another session on
    the same link. `turns_today`/`turns_date` is the daily rate-limit CAS,
    same shape as `EmailTrigger.runs_today`/`runs_date`.
    """

    __tablename__ = "share_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    share_link_id: Mapped[int] = mapped_column(ForeignKey("share_links.id"), nullable=False)
    session_token: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    last_active_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
    turns_today: Mapped[int] = mapped_column(default=0)
    turns_date: Mapped[Optional[str]] = mapped_column(nullable=True)


class ShareMessage(Base):
    """One turn of a ShareSession's human-readable transcript.

    Deliberately separate from the replay-formatted text actually sent as a
    Run's `input` (see `ui/backend/share_chat.py`) -- this is the clean chat
    log the visitor UI and the org's audit view render. `run_id` links a
    turn to the Run that produced it (metering/trace/cancellation all reuse
    the existing `runs` machinery unchanged).
    """

    __tablename__ = "share_messages"
    __table_args__ = (
        UniqueConstraint("share_session_id", "turn_number", name="uq_share_messages_session_turn"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    share_session_id: Mapped[int] = mapped_column(ForeignKey("share_sessions.id"), nullable=False)
    turn_number: Mapped[int]
    role: Mapped[str]  # "user" | "assistant"
    content: Mapped[str]
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class Notification(Base):
    """One thing the customer should know about, in-app and over the webhook.

    Deliberately one table for both: webhook delivery needs retry state and the
    in-app list needs the same rows, so splitting them would immediately
    produce "shown in the app but never delivered" with no single place to
    reconcile it. `delivery_state = "skipped"` means the org configured no
    webhook -- in-app only, which is not a failure.

    `fingerprint` identifies the *problem*, not the occurrence: the health
    evaluator (`ui/backend/trigger_health.py`) uses it to keep a condition
    already alerted for from alerting again until it clears.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    kind: Mapped[str]  # "trigger_health" | "secret_expiry" | "budget"
    severity: Mapped[str]  # "error" | "warning" | "info"
    title: Mapped[str]
    body: Mapped[str]
    fingerprint: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    read_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # "pending" | "delivered" | "failed" | "skipped"
    delivery_state: Mapped[str] = mapped_column(default="pending")
    delivery_attempts: Mapped[int] = mapped_column(default=0)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_delivery_error: Mapped[Optional[str]] = mapped_column(nullable=True)


class OrgNotificationSetting(Base):
    """Where an org wants its notifications delivered, if anywhere.

    The webhook secret lives in the same Fernet scheme as mailbox credentials
    (`ui/backend/secret_store.py`), so the startup check that refuses to boot
    when a rotated key can't read stored credentials covers it too.
    """

    __tablename__ = "org_notification_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, nullable=False
    )
    webhook_url: Mapped[Optional[str]] = mapped_column(nullable=True)
    webhook_secret_encrypted: Mapped[Optional[str]] = mapped_column(nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class OrgRetentionSetting(Base):
    """One org's run-history retention policy, plus proof it is running.

    `run_retention_days` NULL means keep forever -- the default, so an upgrade
    deletes nothing. `last_swept_at`/`last_purged_count` exist because a
    retention policy whose job silently stopped is indistinguishable from one
    that is working, until an audit.
    """

    __tablename__ = "org_retention_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, index=True, nullable=False
    )
    run_retention_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    last_swept_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_purged_count: Mapped[int] = mapped_column(default=0)


class OrgEmailFilterSetting(Base):
    """One org's pre-LLM mail filter rules (Phase 4a).

    An org with no row behaves as `skip_bulk=True` and three empty lists --
    bulk mail is filtered by default. That default is deliberate: the phase
    exists because customers are billed model rates for mail no human wrote,
    and a safety feature nobody switches on protects nobody. It is recoverable:
    one checkbox turns it off, and every filtered message stays visible and
    releasable.
    """

    __tablename__ = "org_email_filter_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, index=True, nullable=False
    )
    skip_bulk: Mapped[bool] = mapped_column(default=True)
    # Lists of patterns; each entry is a full address or `*@domain`. Never a
    # regular expression -- see ui/backend/email_filter.py.
    sender_blocklist: Mapped[list] = mapped_column(JSON, default=list)
    sender_allowlist: Mapped[list] = mapped_column(JSON, default=list)
    subject_blocklist: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class OrgEmailBudgetSetting(Base):
    """One org's customer-facing automation budget (Phase 4a).

    Both caps are NULL by default -- an upgrade must never start refusing to
    process a customer's mail because of a limit they never set. The
    deployment-wide `BESTTEAM_TRIGGER_DAILY_CAP` (runs/day) is a separate,
    operator-owned safety rail and is unaffected by these.
    """

    __tablename__ = "org_email_budget_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), unique=True, index=True, nullable=False
    )
    daily_message_cap: Mapped[Optional[int]] = mapped_column(nullable=True)
    monthly_cost_cap: Mapped[Optional[float]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
