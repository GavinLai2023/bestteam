"""`python -m ui.backend.admin check-env`: the beta launch checklist as code
(beta gate G6).

`check_environment` is a pure function over an environment mapping, so it is
testable without a process and the CLI is one print loop; `check_schema`
takes the database URL (or the historical file path), which is why it is a
second function rather than another branch inside the first. Both only
*read*: nothing here changes a value or starts anything, and neither creates
the database. Run inside the
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
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Union

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

    # --- database -----------------------------------------------------------
    out.append(check_database_url(env))

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


# --- database ------------------------------------------------------------

_DATABASE = "database"
_SUPPORTED_BACKENDS = ("sqlite", "postgresql")


def check_database_url(env: Mapping[str, str]) -> Finding:
    """Which database the backend will open, and whether it can.

    Pure over the environment like the rest of `check_environment`: it parses
    the URL and checks that the driver imports; it never connects.
    `check_schema` is the one that reads.
    """
    try:
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        from .db.database import describe_database_url, resolve_database_url
    except ImportError:
        return Finding("WARN", _DATABASE, "sqlalchemy is not installed, so the database cannot be "
                       "checked (pip install 'bestteam[ui]')")
    url = resolve_database_url(env)
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        return Finding("FAIL", _DATABASE, f"BESTTEAM_DATABASE_URL is not a valid database URL ({exc}). "
                       "Example: postgresql+psycopg://user:password@host:5432/bestteam")
    backend = parsed.get_backend_name()
    if backend not in _SUPPORTED_BACKENDS:
        return Finding("FAIL", _DATABASE, f"BESTTEAM_DATABASE_URL names the {backend!r} engine; "
                       "only sqlite and postgresql are supported")
    if backend != "sqlite":
        try:
            parsed.get_dialect().import_dbapi()
        except (ImportError, ArgumentError) as exc:
            return Finding("FAIL", _DATABASE, f"the {backend} driver is not installed ({exc}); "
                           "install the `ui` extra: pip install 'bestteam[ui]'")
    description = describe_database_url(url)
    if _get(env, "BESTTEAM_DATABASE_URL") and _get(env, "BESTTEAM_DB_PATH"):
        return Finding("WARN", _DATABASE, "both BESTTEAM_DATABASE_URL and BESTTEAM_DB_PATH are set; "
                       f"the URL wins ({description}). Unset BESTTEAM_DB_PATH to avoid confusion")
    return Finding("OK", _DATABASE, description)


def default_database_url(env: Mapping[str, str]) -> str:
    from .db.database import resolve_database_url

    return resolve_database_url(env)


def _as_url(target: Union[str, Path, None]) -> str:
    """Accept the historical file-path argument as well as a URL."""
    from .db.database import resolve_database_url, sqlite_url_for

    if target is None:
        return resolve_database_url({})
    as_text = str(target)
    return as_text if "://" in as_text else sqlite_url_for(as_text)


def _absent_file(url: str) -> bool:
    """True for a SQLite file URL whose file does not exist (nothing to read)."""
    from .db.database import sqlite_path_of

    path = sqlite_path_of(url)
    return path is not None and not path.exists()


def _read_only(url: str):
    from .db.database import readonly_engine

    return readonly_engine(url)


# --- schema version ------------------------------------------------------
#
# Separate from `check_environment`, which is pure over an environment
# mapping and must stay that way. This one reads the database, so it gets
# its own function and its own Finding, and the CLI appends it.

_SCHEMA = "schema"
_DEFAULT_SCRIPT_LOCATION = Path(__file__).resolve().parents[2] / "alembic"


def _stamped_revision(url: str) -> Optional[str]:
    """The database's Alembic revision, or None if it carries no stamp.

    Read-only (`db.database.readonly_engine`), so a checklist run can neither
    create a SQLite file nor write to one that exists --
    `test_check_env_does_not_create_the_database` pins that.
    """
    from sqlalchemy import inspect, text

    engine = _read_only(url)
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                return None
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
    finally:
        engine.dispose()
    return row[0] if row else None


def check_schema(
    target: Union[str, Path, None] = None,
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
    from sqlalchemy.exc import SQLAlchemyError

    from .db.database import MEMORY_URL, describe_database_url, sqlite_path_of

    url = _as_url(target)
    if url == MEMORY_URL:
        return Finding("OK", _SCHEMA, "in-memory database; nothing to migrate")
    if _absent_file(url):
        return Finding("OK", _SCHEMA, f"no database at {sqlite_path_of(url)} yet; the first start creates it at "
                       "the current schema. Run `alembic upgrade head` afterwards to stamp it")

    try:
        from alembic.script import ScriptDirectory
        from alembic.script.revision import RevisionError
    except ImportError:
        return Finding("WARN", _SCHEMA, "alembic is not installed, so the schema version cannot be "
                       "checked (pip install 'bestteam[ui]')")

    try:
        stamped = _stamped_revision(url)
    except (SQLAlchemyError, ImportError) as exc:
        # A SQLite file that cannot be read is a warning; a server that cannot
        # be reached is what the backend itself will die on -- FAIL.
        level = "WARN" if sqlite_path_of(url) is not None else "FAIL"
        return Finding(level, _SCHEMA, f"could not read the schema version from "
                       f"{describe_database_url(url)}: {str(exc).splitlines()[0]}")

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


def check_org_retention(target: Union[str, Path, None] = None) -> Finding:
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import SQLAlchemyError

    from .db.database import MEMORY_URL, describe_database_url

    url = _as_url(target)
    if url == MEMORY_URL or _absent_file(url):
        return Finding("OK", _ORG_RETENTION, "no database yet; nothing to check")

    try:
        engine = _read_only(url)
        try:
            with engine.connect() as conn:
                tables = inspect(conn)
                if not (tables.has_table("organizations") and tables.has_table("org_retention_settings")):
                    return Finding("OK", _ORG_RETENTION, "pre-migration schema; nothing to check")
                uncovered = [row[0] for row in conn.execute(text(
                    "SELECT o.name FROM organizations o "
                    "LEFT JOIN org_retention_settings r ON r.org_id = o.id "
                    "WHERE r.run_retention_days IS NULL ORDER BY o.name"
                ))]
        finally:
            engine.dispose()
    except (SQLAlchemyError, ImportError) as exc:
        return Finding("WARN", _ORG_RETENTION, f"could not read org retention from "
                       f"{describe_database_url(url)}: {str(exc).splitlines()[0]}")

    if uncovered:
        return Finding("WARN", _ORG_RETENTION,
                       f"org(s) keeping run history forever: {', '.join(uncovered)}. "
                       "Set a retention period per org (PUT /api/org/retention) before "
                       "a real customer uses it")
    return Finding("OK", _ORG_RETENTION, "every org has a retention period")


_MODEL_CATALOG = "model-catalog"

# Kept in sync by hand with `db/model_catalog.py::EMBEDDING_TIER` and the
# `fake:`/`fake-architect:` prefixes `adapters/langgraph_adapter.py::_resolve_model`
# understands. This module reads the tables directly rather than importing
# the ORM, the same way `check_org_retention` does.
_EMBEDDING_TIER = "embedding"
_STUB_PREFIXES = ("fake:", "fake-architect:")


def check_model_catalog(target: Union[str, Path, None] = None) -> Finding:
    """WARN when the catalog holds no real chat model.

    The Team Builder wizard runs the Solution Architect on whatever
    `pickDefaultModel()` returns, and that function's last resort is simply
    the first catalog entry. With only stub entries left, that resort picks
    one -- and `fake-architect:` answers the wizard's schemas with a canned
    team, identical for every intent, with no error anywhere. A real
    deployment never seeds a `fake-architect:` entry, but the E2E fixture
    creates exactly this shape if it is ever pointed at a real database
    (observed twice on a dev box), and an admin can delete their way here.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import SQLAlchemyError

    from .db.database import MEMORY_URL, describe_database_url

    url = _as_url(target)
    if url == MEMORY_URL or _absent_file(url):
        return Finding("OK", _MODEL_CATALOG, "no database yet; nothing to check")

    try:
        engine = _read_only(url)
        try:
            with engine.connect() as conn:
                if not inspect(conn).has_table("model_catalog"):
                    return Finding("OK", _MODEL_CATALOG, "pre-migration schema; nothing to check")
                rows = list(conn.execute(text("SELECT spec, tier FROM model_catalog")))
        finally:
            engine.dispose()
    except (SQLAlchemyError, ImportError) as exc:
        return Finding("WARN", _MODEL_CATALOG, f"could not read the model catalog from "
                       f"{describe_database_url(url)}: {str(exc).splitlines()[0]}")

    usable = [
        spec for spec, tier in rows
        if tier != _EMBEDDING_TIER and not str(spec).startswith(_STUB_PREFIXES)
    ]
    if usable:
        return Finding("OK", _MODEL_CATALOG, f"{len(usable)} chat model(s) available to the wizard")

    leftover = ", ".join(sorted(str(spec) for spec, _ in rows)) or "the catalog is empty"
    return Finding("WARN", _MODEL_CATALOG,
                   f"no real chat model in the catalog ({leftover}). The Team Builder "
                   "wizard will fall back to a stub entry and build the same canned team "
                   "for every intent, silently. Add a provider model "
                   "(PUT /api/config/model-catalog/<spec>)")
