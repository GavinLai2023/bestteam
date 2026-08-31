"""`python -m ui.backend.admin check-env`: the beta launch checklist as code
(beta gate G6).

`check_environment` is a pure function over an environment mapping, so it is
testable without a process and the CLI is one print loop; `check_schema`
takes the database file, which is why it is a second function rather than
another branch inside the first. Both only *read*: nothing here changes
a value or starts anything, and neither creates the database. Run inside the
container (`docker compose run
--rm --no-deps backend python -m ui.backend.admin check-env`) so it sees the
same `.env` the backend will, not a copy on the host.

Three levels. FAIL is something the backend would refuse to start with, or
that would leave a customer deployment open or unusable; the command exits 1
on any FAIL. WARN is a default that is fine on a dev box and almost
certainly wrong for a beta org (history kept forever, no error channel).
OK lines are printed too, so the output *is* the filled-in checklist.
"""

from __future__ import annotations

import base64
import binascii
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Union
from urllib.request import pathname2url

from .auth import is_insecure_secret_key

_TRUTHY = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Finding:
    level: str  # "FAIL" | "WARN" | "OK"
    name: str
    message: str


def _get(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def check_environment(env: Mapping[str, str]) -> List[Finding]:
    out: List[Finding] = []

    def fail(name, msg):
        out.append(Finding("FAIL", name, msg))

    def warn(name, msg):
        out.append(Finding("WARN", name, msg))

    def ok(name, msg):
        out.append(Finding("OK", name, msg))

    # --- keys -------------------------------------------------------------
    secret = _get(env, "BESTTEAM_SECRET_KEY")
    if not secret or is_insecure_secret_key(secret):
        fail("BESTTEAM_SECRET_KEY", "unset or a known placeholder; the backend refuses to start. "
             "Generate: python -c \"import secrets; print(secrets.token_hex(32))\"")
    elif len(secret) < 32:
        warn("BESTTEAM_SECRET_KEY", f"only {len(secret)} characters; use 64 hex characters (token_hex(32))")
    else:
        ok("BESTTEAM_SECRET_KEY", "set")

    secrets_key = _get(env, "BESTTEAM_SECRETS_KEY")
    if not secrets_key:
        warn("BESTTEAM_SECRETS_KEY", "unset; required the moment an org connects a mailbox. "
             "Generate: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    elif secrets_key == secret:
        fail("BESTTEAM_SECRETS_KEY", "same value as BESTTEAM_SECRET_KEY; the backend refuses to start")
    else:
        try:
            raw = base64.urlsafe_b64decode(secrets_key.encode("ascii"))
        except (binascii.Error, ValueError, UnicodeEncodeError):
            raw = b""
        if len(raw) != 32:
            fail("BESTTEAM_SECRETS_KEY", "not a Fernet key (32 url-safe base64 bytes)")
        else:
            ok("BESTTEAM_SECRETS_KEY", "set, distinct from the signing key")

    # --- browser-facing URLs ----------------------------------------------
    cors = _get(env, "BESTTEAM_CORS_ORIGINS")
    origins = [o.strip() for o in cors.split(",") if o.strip()]
    if not origins:
        fail("BESTTEAM_CORS_ORIGINS", "unset; only the localhost dev origins are allowed, so the "
             "deployed frontend cannot call the API")
    elif "*" in origins:
        fail("BESTTEAM_CORS_ORIGINS", "wildcard; incompatible with the credentialed share-session "
             "cookie, the backend refuses to start")
    else:
        bad = [o for o in origins if not o.startswith(("http://", "https://")) or o.endswith("/")]
        if bad:
            fail("BESTTEAM_CORS_ORIGINS", f"must be scheme://host[:port] with no trailing slash: {', '.join(bad)}")
        elif any("localhost" in o or "127.0.0.1" in o for o in origins):
            warn("BESTTEAM_CORS_ORIGINS", f"includes a localhost origin: {cors}")
        else:
            ok("BESTTEAM_CORS_ORIGINS", cors)

    for name, scheme in (("VITE_API_BASE", "https://"), ("VITE_WS_BASE", "wss://")):
        value = _get(env, name)
        if not value:
            fail(name, "unset; the frontend image is built with this baked in")
        elif not value.startswith(scheme):
            warn(name, f"{value!r} is not {scheme}; fine only behind TLS termination that rewrites it")
        elif value.endswith("/"):
            fail(name, "must not end with a slash")
        else:
            ok(name, value)

    # --- must be off on a customer deployment -----------------------------
    if _get(env, "BESTTEAM_DEMO_PIPELINES").lower() in _TRUTHY:
        fail("BESTTEAM_DEMO_PIPELINES", "on; every org user would see and run the shipped demo teams "
             "(one of which reads the process-wide mailbox). Unset it on a customer deployment")
    else:
        ok("BESTTEAM_DEMO_PIPELINES", "off")

    email_env = sorted(k for k in env if k.startswith("BESTTEAM_EMAIL_") and (env.get(k) or "").strip())
    if email_env:
        warn("BESTTEAM_EMAIL_*", f"{', '.join(email_env)} configure ONE process-wide mailbox; the backend "
             "refuses to start with more than one org. Prefer per-org `admin set-email`")
    else:
        ok("BESTTEAM_EMAIL_*", "unset (mailboxes are per-org)")

    if _get(env, "BESTTEAM_TRIGGERS_DISABLED").lower() in _TRUTHY:
        warn("BESTTEAM_TRIGGERS_DISABLED", "on; no automatic run will happen on this deployment")

    # --- beta defaults ----------------------------------------------------
    retention = _get(env, "BESTTEAM_RUN_RETENTION_DAYS")
    if not retention:
        warn("BESTTEAM_RUN_RETENTION_DAYS", "unset; a new org keeps run history forever. Set e.g. 90 "
             "before creating the beta org (existing orgs are never retro-fitted)")
    elif not retention.isdigit() or int(retention) <= 0:
        fail("BESTTEAM_RUN_RETENTION_DAYS", f"{retention!r} is not a positive whole number of days")
    else:
        ok("BESTTEAM_RUN_RETENTION_DAYS", f"{retention} days for newly created orgs")

    dsn = _get(env, "BESTTEAM_SENTRY_DSN")
    if not dsn:
        warn("BESTTEAM_SENTRY_DSN", "unset; the only record of a failure will be the container log")
    else:
        problem = _dsn_problem(dsn)
        if problem is None:
            ok("BESTTEAM_SENTRY_DSN", "set; unhandled errors and failed runs are reported")
        elif problem == "no-sdk":
            warn("BESTTEAM_SENTRY_DSN", "set, but sentry-sdk is not installed (pip install 'bestteam[ui]'); "
                 "reporting will be off")
        else:
            fail("BESTTEAM_SENTRY_DSN", f"not a valid DSN ({problem}); the backend refuses to start")

    # The self-service wizard shows its "Enhanced" (semantic search) choice
    # only when an embedding default is set, so leaving it unset is silent:
    # every customer collection is BM25 keyword matching, which scores 0 on a
    # paraphrased or cross-language query by construction. Recommended models
    # and their measured floors: docs/KNOWLEDGE_BASES.md.
    embedding = _get(env, "BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL")
    if not embedding:
        warn("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", "unset; customers get keyword search only — a "
             "reworded or cross-language question finds nothing, and the wizard never offers the "
             "\"Enhanced\" choice. Set e.g. openai:text-embedding-3-small")
    elif embedding.startswith("fake:"):
        fail("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", f"{embedding!r} is the deterministic $0 test "
             "model; its vectors are noise, so a customer collection built on it retrieves nothing "
             "meaningful")
    else:
        ok("BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL", embedding)
        # Only meaningful with an embedding model: reranking sits on the
        # hybrid retrieval that "Enhanced" turns on, so with none configured
        # this would name a knob that cannot apply.
        rerank = _get(env, "BESTTEAM_KB_DEFAULT_RERANK_MODEL")
        if not rerank:
            warn("BESTTEAM_KB_DEFAULT_RERANK_MODEL", "unset; semantic search runs unreranked. Set "
                 "cross-encoder:BAAI/bge-reranker-base — the one model in the release gate that "
                 "holds cross-language ranking (others measurably failed it)")
        else:
            ok("BESTTEAM_KB_DEFAULT_RERANK_MODEL", rerank)

    # `web_search` fails at run time, not at start-up: the tool raises, the
    # adapter turns the exception into tool-result text, and the model is free
    # to answer from its own weights instead. The customer gets a research
    # brief that looks finished and cites nothing.
    if not _get(env, "TAVILY_API_KEY"):
        warn("TAVILY_API_KEY", "unset; any team given the web_search tool degrades silently — the "
             "tool errors mid-run and the model answers from memory instead of the web. Get a key "
             "at https://tavily.com, or leave unset if no team searches the web")
    else:
        ok("TAVILY_API_KEY", "set; web_search is usable")

    if _get(env, "FORWARDED_ALLOW_IPS"):
        ok("FORWARDED_ALLOW_IPS", _get(env, "FORWARDED_ALLOW_IPS"))
    else:
        warn("FORWARDED_ALLOW_IPS", "unset; behind a reverse proxy every login looks like it comes from "
             "the proxy, so the per-address login budget is shared by all users")

    return out


def _dsn_problem(dsn: str) -> Optional[str]:
    """None if `sentry_sdk.init(dsn=...)` would accept it; "no-sdk" if that
    cannot be known here; else the SDK's own one-line reason."""
    try:
        from sentry_sdk.utils import BadDsn, Dsn
    except ImportError:
        return "no-sdk"
    try:
        Dsn(dsn)
    except BadDsn as exc:
        return str(exc)
    return None


def has_failures(findings: List[Finding]) -> bool:
    return any(f.level == "FAIL" for f in findings)


# --- schema version ------------------------------------------------------
#
# Separate from `check_environment`, which is pure over an environment
# mapping and must stay that way. This one needs the database file, so it
# gets its own function and its own Finding, and the CLI appends it.

_SCHEMA = "schema"

# Keep this default in sync with ui/backend/db_session.py::DB_PATH and
# alembic/env.py::_default_db_path.
_DEFAULT_DB_PATH = Path(__file__).parent / "data" / "bestteam.db"
_DEFAULT_SCRIPT_LOCATION = Path(__file__).resolve().parents[2] / "alembic"


def default_db_path(env: Mapping[str, str]) -> Path:
    return Path(_get(env, "BESTTEAM_DB_PATH") or _DEFAULT_DB_PATH)


def _stamped_revision(path: Path) -> Optional[str]:
    """The database's Alembic revision, or None if it carries no stamp.

    Opened read-only through a `file:` URI, so a checklist run can neither
    create the file nor write to one that exists -- `check-env` is documented
    as safe on a box whose database does not exist yet, and
    `test_check_env_does_not_create_the_database` pins that. Read-only still
    reads a live WAL database, which a plain `sqlite3.connect` on a missing
    path would silently create instead.
    """
    uri = "file:" + pathname2url(str(path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        stamped = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not stamped:
            return None
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        con.close()
    return row[0] if row else None


def check_schema(
    db_path: Union[str, Path, None] = None,
    *,
    script_location: Union[str, Path, None] = None,
) -> Finding:
    """Whether the database's schema matches the migrations in this checkout.

    `init_db` runs `create_all`, which creates missing *tables* and never adds
    a column to a table that already exists. So a database left behind head
    boots clean, serves most of the app, and then raises `no such column` from
    whichever feature touches the new column first -- observed 2026-08-23,
    when a dev database two revisions behind failed an ingestion run rather
    than the launch that should have caught it. Hence FAIL: being behind head
    is not a preference, it is a deployment that will break somewhere.
    """
    path = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
    if str(path) == ":memory:":
        return Finding("OK", _SCHEMA, "in-memory database; nothing to migrate")
    if not path.exists():
        return Finding("OK", _SCHEMA, f"no database at {path} yet; the first start creates it at "
                       "the current schema. Run `alembic upgrade head` afterwards to stamp it")

    try:
        from alembic.script import ScriptDirectory
        from alembic.script.revision import RevisionError
    except ImportError:
        return Finding("WARN", _SCHEMA, "alembic is not installed, so the schema version cannot be "
                       "checked (pip install 'bestteam[ui]')")

    try:
        stamped = _stamped_revision(path)
    except sqlite3.Error as exc:
        return Finding("WARN", _SCHEMA, f"could not read the schema version from {path}: {exc}")

    script = ScriptDirectory(str(script_location or _DEFAULT_SCRIPT_LOCATION))
    head = script.get_current_head()

    if stamped is None:
        # `docs/deployment.md` has the operator start the backend and *then*
        # run `alembic upgrade head`. In between, create_all has built the
        # tables at the current models but nothing stamped them, so the next
        # migration has no floor to measure from.
        return Finding("WARN", _SCHEMA, "the database carries no Alembic stamp; create_all built it "
                       f"but no migration has been recorded. Run `alembic upgrade head` (head is {head})")
    if stamped == head:
        return Finding("OK", _SCHEMA, f"at head ({head})")

    try:
        pending = [rev.revision for rev in script.iterate_revisions(head, stamped)]
    except RevisionError:
        # A database written by a newer checkout than the code being launched.
        return Finding("FAIL", _SCHEMA, f"stamped {stamped}, which is not a revision in this "
                       f"checkout (head is {head}). The database is newer than the code -- deploy the "
                       "matching version rather than migrating")
    return Finding("FAIL", _SCHEMA, f"stamped {stamped}, {len(pending)} migration(s) behind head "
                   f"({head}): {', '.join(reversed(pending))}. The backend will start and then fail "
                   "with `no such column` in whichever feature touches a new column first. "
                   "Run `alembic upgrade head`")


# --- org retention --------------------------------------------------------
#
# BESTTEAM_RUN_RETENTION_DAYS only seeds orgs created after it is set, so the
# env check above can say OK while every existing org still keeps run history
# forever. This one reads the live database (read-only, like check_schema)
# and names those orgs.

_ORG_RETENTION = "org-retention"


def check_org_retention(db_path: Union[str, Path, None] = None) -> Finding:
    path = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
    if str(path) == ":memory:" or not path.exists():
        return Finding("OK", _ORG_RETENTION, "no database yet; nothing to check")

    uri = "file:" + pathname2url(str(path)) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            uncovered = [row[0] for row in con.execute(
                "SELECT o.name FROM organizations o "
                "LEFT JOIN org_retention_settings r ON r.org_id = o.id "
                "WHERE r.run_retention_days IS NULL ORDER BY o.name"
            )]
        finally:
            con.close()
    except sqlite3.Error as exc:
        if "no such table" in str(exc):
            return Finding("OK", _ORG_RETENTION, "pre-migration schema; nothing to check")
        return Finding("WARN", _ORG_RETENTION, f"could not read org retention from {path}: {exc}")

    if uncovered:
        return Finding("WARN", _ORG_RETENTION,
                       f"org(s) keeping run history forever: {', '.join(uncovered)}. "
                       "Set a retention period per org (PUT /api/org/retention) before "
                       "a real customer uses it")
    return Finding("OK", _ORG_RETENTION, "every org has a retention period")
