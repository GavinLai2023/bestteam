"""`python -m ui.backend.admin check-env`: the beta launch checklist as code
(beta gate G6).

A pure function over an environment mapping, so it is testable without a
process and the CLI is one print loop. It only *reads*: nothing here changes
a value or starts anything. Run inside the container (`docker compose run
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
from typing import List, Mapping, Optional

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
