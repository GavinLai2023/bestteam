"""FastAPI backend for the bestteam runtime monitoring dashboard.

Pure wrapper: every endpoint is a thin shim over the bestteam SDK
(`load_workflow`, `Workflow.run/.stream/.visualize`). The only thing this
layer adds is turning a blocking, synchronous SDK into something a browser
can subscribe to live — an in-memory run registry plus a thread-pool bridge
from LangGraph's blocking `.stream()` generator into asyncio.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from bestteam import Workflow, load_workflow
from bestteam.core.loader import _build_workflow
from bestteam.exceptions import BestTeamError

from . import auth
from . import automation_results
from . import email_trigger
from . import interview
from . import secret_store
from .auth_api import get_current_org, get_current_user, router as auth_router
from .builder import router as builder_router
from .crud import public_router as catalog_read_router, router as crud_router
from .email_trigger_api import router as email_trigger_router
from .interview import router as interview_router
from .admin_api import router as admin_router
from .memory_api import router as memory_router
from .org_settings import router as org_settings_router
from .db.models import (
    KnowledgeBaseRecord,
    OrgEmailCredential,
    Organization,
    Run,
    SkillRecord,
    TraceEventRecord,
    User,
    WorkflowRecord,
)
from .db.email_credentials import ensure_secrets_key_for_stored_credentials
from .db.users import get_user_by_username, orgs_with_multiple_members
from .db_session import SessionLocal, get_db
from .email_tools import load_email_tools
from .knowledge_bases import (
    contain_workflow_config_for_load,
    ensure_workflow_cache_paths_for_source,
    load_knowledge_base_tools,
)
from .runtime import _executor, registry, run_in_background
from .skills import load_skills
from .ws_tickets import consume_ticket, issue_ticket

WORKFLOWS_DIR = Path(__file__).parent / "workflows"


def demo_workflows_enabled() -> bool:
    """Whether the shipped YAML workflows in `WORKFLOWS_DIR` are exposed.

    Off by default. Those files are *our* demo fixtures, not any customer's
    teams: most run `fake:` models that emit a hardcoded, plausible-looking
    answer regardless of input, and the `*_live` ones spend real API quota --
    `email_triage_demo_live` reads the running org's connected mailbox (its
    per-org stored credentials), falling back to the `BESTTEAM_EMAIL_*` env
    mailbox on a single-org deployment. They are global (no `org_id`), so while
    this is on, every org user on the deployment sees and can run all of them.

    Turn it on (`BESTTEAM_DEMO_WORKFLOWS=1`) only on a dev box or a sales-demo
    instance. This gates the UI backend only -- the SDK/CLI YAML path
    (`load_workflow(path)`) is unaffected and always works.

    Read per-call rather than at import so a deployment (or a test) can flip it
    without re-importing the module.
    """
    return os.environ.get("BESTTEAM_DEMO_WORKFLOWS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

if auth.is_insecure_secret_key(auth.SECRET_KEY):
    raise RuntimeError(
        "BESTTEAM_SECRET_KEY is unset or still a known placeholder value. "
        "Set BESTTEAM_SECRET_KEY to a long random value before starting this service "
        "-- generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

email_trigger.validate_trigger_env()

def _enforce_one_member_per_org_or_raise(db) -> None:
    """Raise if any org has multiple members (the one-member-per-org invariant).

    `create_all` never adds the enforcing index to a pre-existing `users`
    table, so a database created under the earlier multi-member architecture
    (and started before `alembic upgrade head`) would otherwise serve the
    mailbox-management routes with no per-member separation. Run from the ASGI
    lifespan (below) so real serving is refused while the operator CLI --
    delete-user / move-user recovery, which doesn't import this module -- still
    runs. It is NOT run at import so tools/tests that merely import the app
    aren't coupled to the live database's contents.
    """
    duplicates = orgs_with_multiple_members(db)
    if duplicates:
        offending = ", ".join(f"org_id={oid} ({n} members)" for oid, n in duplicates)
        raise RuntimeError(
            "One member per org is required, but these orgs have multiple members: "
            f"{offending}. This database predates the one-member-per-org rule. "
            "Resolve it with the operator CLI (admin delete-user / move-user), run "
            "`alembic upgrade head`, then restart -- the CLI still runs while HTTP "
            "serving is blocked (see docs/deployment.md)."
        )


@asynccontextmanager
async def _lifespan(_app):
    """ASGI startup: refuse to serve while the membership invariant is violated,
    then run the autonomous email-trigger poller for the app's lifetime.

    The membership guard is data-dependent (unlike the config guards below), so
    it runs when the server actually starts serving, not at import."""
    with SessionLocal() as session:
        try:
            _enforce_one_member_per_org_or_raise(session)
        except OperationalError:
            pass  # pre-migration schema (no users table yet); nothing to enforce
    stop_polling = asyncio.Event()
    poller = asyncio.create_task(
        email_trigger.poll_forever(stop_polling, email_trigger.build_trigger_workflow)
    )
    try:
        yield
    finally:
        stop_polling.set()
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller


# Secrets-store guards run at app import, NOT in db_session, so the operator CLI
# -- which imports db_session but not main -- can still run recovery commands
# (`admin clear-email`/`set-email`) when the key can't decrypt.
secret_store.ensure_key_separation()
with SessionLocal() as _startup_session:
    try:
        ensure_secrets_key_for_stored_credentials(_startup_session)
    except OperationalError:
        pass  # pre-migration schema; db_session already warned about seeding

_default_cors_origins = "http://localhost:5173,http://127.0.0.1:5173"
_cors_origins = [o.strip() for o in os.environ.get("BESTTEAM_CORS_ORIGINS", _default_cors_origins).split(",") if o.strip()]

app = FastAPI(title="bestteam monitoring dashboard", lifespan=_lifespan)

# Generous ceiling on any request body; the interview endpoint keeps its own
# tighter limit. Reverse-proxy client_max_body_size remains the authoritative
# hard bound -- this is the application-level backstop.
_GENERAL_MAX_BODY_BYTES = 512 * 1024 * 1024


class _RequestBodyTooLarge(Exception):
    """Internal signal raised before Starlette can parse or spool a body."""


class _RequestBodyLimitMiddleware:
    """Enforce declared and streamed request-size limits at the ASGI boundary.

    Function-based middleware can reject a truthful ``Content-Length``, but
    multipart parsing otherwise begins before the endpoint can cap reads. This
    wrapper also counts every ASGI body chunk, covering chunked and understated
    requests before Starlette's multipart parser can spool the excess (CR-009).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        limit = interview._MAX_UPLOAD_BYTES if path.endswith("/interview/transcribe") else _GENERAL_MAX_BODY_BYTES
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > limit:
                await _send_body_too_large(send, limit)
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await _send_body_too_large(send, limit)


async def _send_body_too_large(send, limit: int) -> None:
    body = f'{{"detail":"Request body exceeds the {limit // (1024 * 1024)} MB limit."}}'.encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# Add the body limiter first so CORS is the outermost layer and a 413 from the
# size check still gets CORS headers.
app.add_middleware(_RequestBodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(builder_router)
app.include_router(interview_router)
app.include_router(crud_router)
app.include_router(catalog_read_router)
app.include_router(memory_router)
app.include_router(admin_router)
app.include_router(org_settings_router)
app.include_router(email_trigger_router)

logger = logging.getLogger("bestteam.api")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for anything not already turned into an HTTPException by a
    route handler (BestTeamError/ValidationError/KeyError/TypeError are
    handled inline and never reach here). Logs the full traceback
    server-side and returns a generic, non-leaking 500 to the client."""
    logger.exception("Unhandled exception in %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Keyed by (org_id, workflow name): two orgs' same-named workflows are
# different entries, so one org's cached build can never serve another org.
# YAML demo workflows are global and cache under (None, name).
_workflow_cache: Dict[Tuple[Optional[int], str], Tuple[Workflow, Any]] = {}
# Guards the cache dict and a monotonic generation counter. `_get_workflow`
# snapshots the generation before it builds and only stores the result if the
# generation hasn't advanced -- so a concurrent KB/skill invalidation (which
# bumps the generation, see crud._invalidate_workflow_cache) can't be undone by
# a load that started before it and finished after (CR-005).
_workflow_cache_lock = threading.Lock()
_workflow_cache_generation = 0


def _store_workflow_in_cache(
    key: Tuple[Optional[int], str], workflow: Workflow, cache_key: Any, generation: int
) -> None:
    with _workflow_cache_lock:
        if generation == _workflow_cache_generation:
            _workflow_cache[key] = (workflow, cache_key)


class RunRequest(BaseModel):
    workflow: str
    input: str


def _dependency_freshness(db: Session) -> Tuple[Any, ...]:
    """Fingerprint of all SkillRecords and KnowledgeBaseRecords, folded into a
    cached Workflow's cache key so any change to either invalidates a cached
    workflow that might depend on them.

    Includes the row COUNT as well as max(updated_at): a *deletion* doesn't
    change the maximum, so a max-only fingerprint would keep serving a deleted
    dependency until the explicit cache clear -- and miss it entirely in the
    window between the mutation's commit and that clear. Counting makes the
    fingerprint reflect deletes from the reader's own committed DB view, so a
    stale cache entry is a natural miss regardless of invalidation timing
    (CR-005). (Adds/updates already bump `updated_at` to now, moving the max.)

    Deliberately global rather than scoped to only the names a given
    workflow references -- see
    docs/superpowers/specs/2026-06-22-code-review-fixes-design.md, "Design
    > A" for why."""
    skills_count, skills_max = db.query(
        func.count(SkillRecord.id), func.max(SkillRecord.updated_at)
    ).one()
    kb_count, kb_max = db.query(
        func.count(KnowledgeBaseRecord.id), func.max(KnowledgeBaseRecord.updated_at)
    ).one()
    # Per-org email tools are baked into the compiled workflow, so connecting /
    # rotating / clearing a mailbox must invalidate that org's cached workflows.
    email_count, email_max = db.query(
        func.count(OrgEmailCredential.id), func.max(OrgEmailCredential.updated_at)
    ).one()
    return (skills_count, skills_max, kb_count, kb_max, email_count, email_max)


def _resolve_workflow_and_version(
    name: str, db: Optional[Session] = None, org_id: Optional[int] = None
) -> tuple[Workflow, Optional[int]]:
    """Load a workflow by name for one org and return it together with the
    `current_version_id` of the record it was built from (None for a YAML demo
    or an absent record). Returning the version from the SAME record read that
    produces the config makes a run's stamped version match the config it
    executed even if a redeploy commits concurrently -- a separate
    `current_version_id` re-query could observe a newer version than the one
    built (pysqlite does not lock on SELECT).

    Load and cache a workflow by name for one org (Workflow already
    memoizes its own compiled graph, so repeat runs of the same workflow
    stay cheap).

    A `WorkflowRecord` in the database (e.g. one deployed via the Team
    Builder Wizard, or edited through the `/api/config` CRUD API) is looked
    up within `org_id` -- another org's same-named workflow is invisible --
    and is cached on its `updated_at` under the `(org_id, name)` cache key.
    Otherwise falls back to `WORKFLOWS_DIR/<name>.yaml` (global demo
    workflows, shared across orgs under `(None, name)`), cached by the
    file's mtime, so editing a workflow file on disk is picked up on the
    next request.

    `db` is the request's `get_db`-provided session, if available, so this
    sees the same data as the `/api/builder` and `/api/config` routers
    (including in tests, which override `get_db`); if omitted, a one-off
    session against the module-level engine is used."""
    source = WORKFLOWS_DIR / f"{name}.yaml"
    # Snapshot the invalidation generation before we read dependencies / build,
    # so a KB/skill delete that races this load makes us skip caching a stale
    # result rather than repopulating the cache after the invalidation (CR-005).
    generation = _workflow_cache_generation
    if db is not None:
        record = db.query(WorkflowRecord).filter_by(name=name, org_id=org_id, status="deployed").one_or_none()
        dependency_freshness = _dependency_freshness(db) if record is not None else None
    else:
        with SessionLocal() as session:
            record = session.query(WorkflowRecord).filter_by(name=name, org_id=org_id, status="deployed").one_or_none()
            dependency_freshness = _dependency_freshness(session) if record is not None else None

    if record is not None:
        cache_key: Any = ("db", record.updated_at, *dependency_freshness)
        cached = _workflow_cache.get((org_id, name))
        if cached is not None and cached[1] == cache_key:
            return cached[0], record.current_version_id
        # Only load skills and build standalone KB tools (which may re-chunk
        # files and, for type: vector, call a paid embedding model) on a cache
        # miss -- not on every request. Dependencies resolve within the
        # workflow record's own org (+ platform built-in skills), so one org's
        # workflow can never pull in another org's KB or skill by name.
        try:
            # Tool loading is inside the try so a BestTeamError from resolving a
            # standalone KB (e.g. a legacy KB shadowing a built-in, F4) becomes a
            # clean 400 like a build error, not an uncaught 500.
            if db is not None:
                skill_lookup = load_skills(db, record.org_id)
                kb_tools = load_knowledge_base_tools(db, record.config, source, org_id=record.org_id)
                email_tools = load_email_tools(db, record.org_id)
            else:
                with SessionLocal() as session:
                    skill_lookup = load_skills(session, record.org_id)
                    kb_tools = load_knowledge_base_tools(
                        session, record.config, source, org_id=record.org_id
                    )
                    email_tools = load_email_tools(session, record.org_id)
            config = contain_workflow_config_for_load(record.config)
            ensure_workflow_cache_paths_for_source(config, source)
            workflow = _build_workflow(
                config,
                source=source,
                # Per-org email tools override the env-based ones in REGISTRY by
                # name, so this org's agents reach this org's mailbox.
                extra_tools={**kb_tools, **email_tools},
                extra_skills=skill_lookup,
            )
        except (KeyError, TypeError, BestTeamError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _store_workflow_in_cache((org_id, name), workflow, cache_key, generation)
        return workflow, record.current_version_id

    # Same 404 whether the demos are disabled or the file is absent -- hiding
    # them from the list alone would still leave them runnable by name via
    # POST /api/runs, which for email_triage_demo_live means a customer's agent
    # reaching the configured mailbox.
    path = WORKFLOWS_DIR / f"{name}.yaml"
    if not demo_workflows_enabled() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"Unknown workflow '{name}'")

    # A global YAML demo can reference the built-in email_triage_reply skill,
    # whose email_* tools otherwise resolve to the env-based REGISTRY. Inject the
    # requesting org's mailbox tools and cache per (org_id, name): a global
    # (None, name) cache would let one org reuse another org's baked-in mailbox.
    # org_id None keeps the shared global cache and the env/REGISTRY tools (the
    # SDK / single-org path). YAML demos reference only platform built-in skills
    # (org NULL), never an org's own skills or knowledge bases.
    own_session = db is None
    session = SessionLocal() if own_session else db
    try:
        mtime = path.stat().st_mtime
        if org_id is not None:
            cache_key = ("file", mtime, *_dependency_freshness(session))
            email_tools = load_email_tools(session, org_id)
        else:
            cache_key = ("file", mtime)
            email_tools = {}
        cached = _workflow_cache.get((org_id, name))
        if cached is not None and cached[1] == cache_key:
            return cached[0], None
        builtin_skills = load_skills(session, None)
        try:
            workflow = load_workflow(
                path,
                toolkits=[email_tools] if email_tools else None,
                skills=list(builtin_skills.values()),
            )
        except BestTeamError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if own_session:
            session.close()
    _store_workflow_in_cache((org_id, name), workflow, cache_key, generation)
    return workflow, None


def _get_workflow(name: str, db: Optional[Session] = None, org_id: Optional[int] = None) -> Workflow:
    """Back-compat wrapper: the built workflow only (see
    `_resolve_workflow_and_version` for the version-aware form used by run
    creation)."""
    return _resolve_workflow_and_version(name, db, org_id)[0]


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/workflows")
def list_workflows(db: Session = Depends(get_db), org: Organization = Depends(get_current_org)):
    # The org's own deployed/edited workflows, plus the global YAML demos only
    # where they're deliberately enabled (see `demo_workflows_enabled`).
    db_names = {
        row.name
        for row in db.query(WorkflowRecord.name).filter(
            WorkflowRecord.org_id == org.id, WorkflowRecord.status == "deployed"
        )
    }
    yaml_names = (
        {p.stem for p in WORKFLOWS_DIR.glob("*.yaml")} if demo_workflows_enabled() else set()
    )
    return {"workflows": sorted(db_names | yaml_names)}


@app.get("/api/workflows/{name}/graph")
def workflow_graph(name: str, db: Session = Depends(get_db), org: Organization = Depends(get_current_org)):
    workflow = _get_workflow(name, db, org.id)
    try:
        return {"mermaid": workflow.visualize()}
    except BestTeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs")
async def create_run(
    req: RunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
):
    # Resolve the workflow and the version it was built from together, so the
    # run is stamped with exactly the version whose config it executes.
    workflow, version_id = _resolve_workflow_and_version(req.workflow, db, org.id)
    run = registry.create(req.workflow, req.input, org_id=org.id, username=user.username)

    _executor.submit(
        run_in_background,
        run.id,
        workflow,
        req.input,
        engine=db.get_bind(),
        user_id=user.username,
        org_id=org.id,
        principal_id=user.principal_id,
        username=user.username,
        workflow_version_id=version_id,
    )

    return {"run_id": run.id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: User = Depends(get_current_user)):
    run = registry.get(run_id)
    # Cross-org access is a 404, not a 403 -- existence is not revealed.
    # Org-less platform admins pass (operator debugging; an org-bound
    # is_admin flag does NOT qualify, CR-030); runs with org_id=None (e.g.
    # builder sandbox runs) are visible to admins and org-less callers only.
    is_platform_admin = user.is_admin and user.org_id is None
    if run is None or (run.org_id != user.org_id and not is_platform_admin):
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'")
    return dataclasses.asdict(run)


@app.get("/api/runs/{run_id}/trace")
def get_run_trace(run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The persisted trace-event history for a run (Activity page's run-detail
    view). Serves finished/historical runs -- a still-`running` run's live
    detail view uses the WebSocket stream instead; this endpoint doesn't merge
    with the in-memory registry."""
    run = db.get(Run, run_id)
    is_platform_admin = user.is_admin and user.org_id is None
    if run is None or (run.org_id != user.org_id and not is_platform_admin):
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'")
    rows = (
        db.query(TraceEventRecord)
        .filter(TraceEventRecord.run_id == run_id)
        .order_by(TraceEventRecord.seq)
        .all()
    )
    return {
        "events": [
            {
                "seq": row.seq,
                "type": row.type,
                "agent": row.agent,
                "data": json.loads(row.data) if row.data is not None else None,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, user: User = Depends(get_current_user)):
    """Ask a running run to stop cooperatively (`registry.request_cancel`).
    Cancellation only takes effect between yielded events -- a run mid-node
    finishes that node first, so this doesn't stop instantly."""
    run = registry.get(run_id)
    is_platform_admin = user.is_admin and user.org_id is None
    if run is None or (run.org_id != user.org_id and not is_platform_admin):
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'")
    if not registry.request_cancel(run_id):
        raise HTTPException(status_code=409, detail="Run already finished")
    return JSONResponse(status_code=202, content={"status": "cancel_requested"})


@app.post("/api/runs/{run_id}/retry")
def retry_run(
    run_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Safely retry a failed/errored autonomous email-triggered run over its
    exact original UID batch (spec section 11.2) -- always a NEW run, never an
    overwrite of history. Only eligible for a run this org owns that carries a
    recorded `trigger_context`; see `email_trigger.retry_triggered_run` for the
    full eligibility checks (UIDVALIDITY match, mailbox reachable, workflow
    still buildable, daily cap)."""
    run_row = db.get(Run, run_id)
    if run_row is None or run_row.org_id != org.id:
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'")
    try:
        new_run_id = email_trigger.retry_triggered_run(db, run_row)
    except email_trigger.RetryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run_id": new_run_id}


@app.get("/api/runs")
def list_runs(
    run_id: Optional[str] = None,
    workflow: Optional[str] = None,
    status: Optional[str] = None,
    manual: Optional[bool] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Manual + automatic run history for the Activity page's Runs tab.
    Lightweight rows only (no input/output) -- full detail is `GET
    /api/runs/{id}` and `GET /api/runs/{id}/trace`.

    Bounded (`limit`/`offset`, default 50, max 200): autonomous runs
    accumulate indefinitely, so an unbounded query would grow DB work,
    response size, and browser memory without limit as an org's history
    grows. `total` is returned so a future "load more"/pager can work off it.

    `run_id`: this DB-backed, org-scoped route (unlike `GET /api/runs/{id}`,
    which only ever sees the in-memory registry) is how the Activity page's
    Needs-attention list resolves a specific run's real, persisted status
    before opening it -- it must never assume `completed` for a run that
    could actually be `failed` (Codex review finding)."""
    query = db.query(Run).filter(Run.org_id == org.id)
    if run_id:
        query = query.filter(Run.id == run_id)
    if workflow:
        query = query.filter(Run.workflow == workflow)
    if status:
        query = query.filter(Run.status == status)
    if manual is not None:
        if manual:
            # `!=` is UNKNOWN (excluded) for a NULL username in SQL -- a
            # legacy/pre-migration row with no recorded username is reported
            # autonomous: False below, so it must match here too.
            query = query.filter(or_(Run.username != email_trigger.TRIGGER_USERNAME, Run.username.is_(None)))
        else:
            query = query.filter(Run.username == email_trigger.TRIGGER_USERNAME)
    if since is not None:
        query = query.filter(Run.created_at >= since)
    if until is not None:
        query = query.filter(Run.created_at <= until)
    total = query.count()
    rows = query.order_by(Run.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "runs": [
            {
                "id": row.id,
                "workflow": row.workflow,
                "status": row.status,
                "started_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(),
                "username": row.username,
                "autonomous": row.username == email_trigger.TRIGGER_USERNAME,
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/automation-results")
def list_automation_results_endpoint(
    run_id: Optional[str] = None,
    needs_attention: Optional[bool] = None,
    status: Optional[str] = None,
    result_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Org-scoped, filterable, paginated Property Maintenance Inbox results --
    the Activity page's Needs-attention list, and (filtered by `run_id`) Run
    Detail's automation-results section (spec section 12.1). Never returns raw
    email bodies; `payload` is the capped, validated extraction already
    written by `automation_results.normalize_run_result`."""
    rows, total = automation_results.list_automation_results(
        db, org.id, run_id=run_id,
        needs_attention=needs_attention, status=status, result_type=result_type,
        limit=limit, offset=offset,
    )
    return {
        "results": [
            {
                "id": row.id,
                "run_id": row.run_id,
                "source_type": row.source_type,
                "result_type": row.result_type,
                "status": row.status,
                "needs_attention": row.needs_attention,
                "payload": row.payload,
                "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/automation-results/summary")
def automation_results_summary_endpoint(
    date: Optional[str] = None,
    tz_offset_minutes: int = 0,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    """Today's (or `date`, an ISO `YYYY-MM-DD` string) Property Maintenance
    Inbox counters for the Activity page's summary card (spec section 6.2).
    `tz_offset_minutes` is the browser's `Date.getTimezoneOffset()`, used to
    bound `date` by the caller's local day instead of a UTC day (Codex
    review finding) -- see `automation_results.summary_for_date`."""
    if date:
        try:
            day = _date.fromisoformat(date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date must be an ISO date (YYYY-MM-DD)") from exc
    else:
        day = datetime.now(timezone.utc).date()
    return automation_results.summary_for_date(db, org.id, day, tz_offset_minutes=tz_offset_minutes)


@app.post("/api/runs/ws-ticket")
def create_ws_ticket(user: User = Depends(get_current_user)) -> Dict[str, str]:
    """Exchange the caller's bearer token for a short-lived, single-use ticket
    to authenticate a WebSocket stream connection (CR-013). Only the ticket --
    never the long-lived bearer -- goes in the stream URL."""
    return {"ticket": issue_ticket(user.username, user.security_stamp)}


def _stream_access(db: Session, username: str, ticket_stamp: Optional[str], run: Any) -> bool:
    """True if the ticket's principal may still receive `run`'s events.

    Re-checked before every send so a mid-stream lifecycle change stops
    delivery immediately (review r-ext #2, r-ext2 #3): the user was deleted,
    their password was reset or the username was recreated (security stamp no
    longer matches the ticket's), they were moved to another org (would be
    cross-tenant disclosure), or their org was deactivated. Org-less platform
    admins pass through for operator debugging (an org-bound is_admin flag does
    NOT qualify, CR-030)."""
    user = get_user_by_username(db, username)
    if user is None:
        return False
    # Stamp bound at ticket issuance must still match the account's current one.
    if user.security_stamp != ticket_stamp:
        return False
    is_platform_admin = user.is_admin and user.org_id is None
    if not is_platform_admin:
        if run.org_id != user.org_id:
            return False
        org = db.get(Organization, user.org_id) if user.org_id is not None else None
        if org is None or not org.active:
            return False
    return True


@app.websocket("/api/runs/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str, ticket: Optional[str] = None, db: Session = Depends(get_db)):
    """Replays any events already produced, then relays new ones live until
    the run reaches a terminal state (run_completed / run_failed).

    Authenticated with a short-lived, single-use `?ticket=` minted by
    `POST /api/runs/ws-ticket` -- browsers can't set custom headers when
    opening a WebSocket, and putting the long-lived bearer in the URL leaks it
    to logs/history (CR-013)."""
    resolved = consume_ticket(ticket) if ticket else None
    if resolved is None:
        await websocket.close(code=4401)
        return
    username, ticket_stamp = resolved

    engine = db.get_bind()

    run = registry.get(run_id)
    # Same close code (4404) for unknown run and for any authorization failure
    # (cross-org, deactivated org, deleted user, stale ticket), so a probing
    # client gets no existence oracle. An invalid ticket already closed 4401.
    if run is None or not _stream_access(db, username, ticket_stamp, run):
        db.close()
        await websocket.close(code=4404)
        return

    # Release the request DB connection now -- `Depends(get_db)` would otherwise
    # hold it open for the whole (possibly long) streaming connection. Per-event
    # re-authorization below opens its own short-lived session on `engine`.
    db.close()

    await websocket.accept()
    subscriber_queue = registry.subscribe(run_id)
    if subscriber_queue is None:
        # The run existed at the check above but was evicted (bounded
        # RunRegistry eviction, see registry.py) before subscribe() ran --
        # same close code as "never existed", for the same reason: no
        # cross-org/eviction-timing oracle for a probing client.
        await websocket.close(code=4404)
        return
    try:
        while True:
            event = await subscriber_queue.get()
            # Re-authorize before delivering: a lifecycle change since connect
            # (move/deactivate/delete/password-reset/username-reuse) must stop
            # the stream rather than leak further events (review r-ext #2).
            with Session(engine) as check_db:
                if not _stream_access(check_db, username, ticket_stamp, run):
                    await websocket.close(code=4404)
                    return
            await websocket.send_json(event)
            if event["type"] in ("run_completed", "run_failed", "run_cancelled"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        registry.unsubscribe(run_id, subscriber_queue)
