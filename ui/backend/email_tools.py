"""Build per-org email tools for pipeline loading (per-org secrets store).

Mirrors `knowledge_bases.py::load_knowledge_base_tools`: returns a name -> tool
mapping merged into a pipeline's `extra_tools`, where it overrides the
env-based `email_*` tools (`EMAIL_TOOL_NAMES`) in `REGISTRY` by name
(`core/loader.py`). So a run for org A resolves org A's mailbox and never org
B's.

Resolution order for one org:
- has stored credentials  -> tools bound to that org's mailbox;
- no credentials, but BESTTEAM_EMAIL_BACKEND is set -> `{}` (the process-env
  single-mailbox path stays in effect; used by single-org deployments and the
  SDK/CLI, where env email is the supported model);
- no credentials and no env backend -> tools that return a friendly
  "no mailbox connected" message, so a pipeline referencing the built-in
  email_triage_reply skill still compiles and runs on a multi-org deployment.
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any, Dict

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from bestteam.tools._oauth import MicrosoftClientCredentialsToken
from bestteam.tools.email_client import (
    _ImapBackend,
    email_draft_reply,
    email_find,
    email_read,
    email_read_attachment,
    make_email_tools,
)

from . import secret_store
from .db.email_credentials import AUTH_MICROSOFT_OAUTH, get_email_credentials

_logger = logging.getLogger(__name__)

# Every name `make_email_tools` can return. A name missing here is never
# overridden by the per-org tools (see the module docstring), so a pipeline
# using only that tool resolves as "does not use email": no per-org tools are
# built, the run falls through to the process-env mailbox, and the wizard never
# asks the customer to connect one. Pinned structurally in
# tests/test_load_email_tools.py -- it is a separate set from
# deploy_validation.EMAIL_TOOL_NAMES only because the SDK cannot import from
# ui/backend/.
EMAIL_TOOL_NAMES = frozenset(
    {"email_find", "email_read", "email_read_attachment", "email_draft_reply"}
)


def resolve_agent_tool_sets(
    db: Session,
    spec_raw: Dict[str, Any],
    org_id: int,
    *,
    pipeline_version_id: int | None = None,
) -> list:
    """`[(agent_name, {tool names}), ...]` with each agent's tools resolved
    through the skills it references.

    Tools reach an agent either directly (its `tools:`) or via a skill
    (`skills:`, e.g. the built-in `email_triage_reply`), so any capability
    question about an agent has to resolve both. Shared by `spec_uses_email`
    and the deploy-time egress-conflict check so the two can never disagree
    about what an agent can actually do.
    """
    from .skills import load_skills

    skills = load_skills(db, org_id, pipeline_version_id=pipeline_version_id)
    resolved = []
    for index, agent in enumerate(spec_raw.get("agents", []) or []):
        if not isinstance(agent, dict):
            continue
        names = set(agent.get("tools", []) or [])
        for skill_name in agent.get("skills", []) or []:
            skill = skills.get(skill_name)
            if skill is not None:
                names.update(skill.tools)
        resolved.append((agent.get("name") or f"#{index}", names))
    return resolved


def spec_uses_email(
    db: Session,
    spec_raw: Dict[str, Any],
    org_id: int,
    *,
    pipeline_version_id: int | None = None,
) -> bool:
    """True if any agent in a Specification resolves to an email tool.

    Used by the wizard to decide whether to ask the customer to connect a
    mailbox (and to gate deploy).
    """
    return any(
        names & EMAIL_TOOL_NAMES
        for _name, names in resolve_agent_tool_sets(
            db, spec_raw, org_id, pipeline_version_id=pipeline_version_id
        )
    )

_NOT_CONNECTED = (
    "No mailbox is connected for your team yet. Ask an admin to connect one "
    "before using the email tools."
)
_UNREADABLE = (
    "The connected mailbox can't be read right now (its stored credentials "
    "could not be decrypted). Ask an admin to reconnect it."
)


def _fixed_message_tools(message: str) -> Dict[str, Any]:
    """One tool per `EMAIL_TOOL_NAMES` that ignores its input and returns `message`.

    Keeps the public names/docstrings so a pipeline referencing the built-in
    email skill still compiles, while communicating the state to the model.
    Covering every name matters as much as the set itself does: a tool with no
    placeholder is not overridden either, so it falls through to the process-env
    mailbox for an org that has none of its own.
    """

    @functools.wraps(email_find)
    def find(query: str = "") -> str:
        return message

    @functools.wraps(email_read)
    def read(message_id: str) -> str:
        return message

    @functools.wraps(email_read_attachment)
    def read_attachment(message_id: str, filename: str) -> str:
        return message

    @functools.wraps(email_draft_reply)
    def draft_reply(message_id: str, body: str) -> str:
        return message

    return {
        "email_find": find,
        "email_read": read,
        "email_read_attachment": read_attachment,
        "email_draft_reply": draft_reply,
    }


def build_org_imap_backend(db: Session, org_id: int):
    """The org's IMAP backend from stored credentials, or None if unconnected.

    Decrypts the stored secret; raises secret_store.SecretsKeyError /
    InvalidToken on a bad/rotated key (the caller decides how to surface that).
    `auth_type` chooses how the connection authenticates -- Exchange Online
    mailboxes use an app-only OAuth token because basic auth is gone there.
    """
    cred = get_email_credentials(db, org_id)
    if cred is None:
        return None
    secret = secret_store.decrypt(cred.password_encrypted)
    if cred.auth_type == AUTH_MICROSOFT_OAUTH:
        return _ImapBackend(
            host=cred.host,
            user=cred.username,
            port=cred.port,
            drafts=cred.drafts_folder,
            restrict_to_public=True,
            token_provider=MicrosoftClientCredentialsToken(
                tenant_id=cred.oauth_tenant_id or "",
                client_id=cred.oauth_client_id or "",
                client_secret=secret,
            ),
        )
    return _ImapBackend(
        host=cred.host,
        user=cred.username,
        password=secret,
        port=cred.port,
        drafts=cred.drafts_folder,
        restrict_to_public=True,  # customer-supplied host: validate + pin on connect
    )


def load_email_tools(db: Session, org_id: int) -> Dict[str, Any]:
    """Return the email tools for `org_id` (see module docstring for the order).

    Cheap: builds the backend object but opens no connection (IMAP connects
    lazily per operation), so this is safe to call on every pipeline build.
    """
    cred = get_email_credentials(db, org_id)
    if cred is not None:
        try:
            backend = build_org_imap_backend(db, org_id)
        except (InvalidToken, secret_store.SecretsKeyError):
            # A wrong/rotated key must not crash the pipeline build for this (or
            # any other) org -- surface it as a clear tool-level message and log
            # server-side. Startup validation normally catches this first.
            _logger.warning("Could not decrypt email credentials for org_id=%s", org_id)
            return _fixed_message_tools(_UNREADABLE)
        return make_email_tools(backend)
    if os.environ.get("BESTTEAM_EMAIL_BACKEND", "").strip():
        # Single-org / SDK env path handles email; don't shadow the env tools.
        return {}
    return _fixed_message_tools(_NOT_CONNECTED)
