"""Build per-org email tools for workflow loading (per-org secrets store).

Mirrors `knowledge_bases.py::load_knowledge_base_tools`: returns a name -> tool
mapping merged into a workflow's `extra_tools`, where it overrides the
env-based `email_find`/`email_read`/`email_draft_reply` in `REGISTRY` by name
(`core/loader.py`). So a run for org A resolves org A's mailbox and never org
B's.

Resolution order for one org:
- has stored credentials  -> tools bound to that org's mailbox;
- no credentials, but BESTTEAM_EMAIL_BACKEND is set -> `{}` (the process-env
  single-mailbox path stays in effect; used by single-org deployments and the
  SDK/CLI, where env email is the supported model);
- no credentials and no env backend -> tools that return a friendly
  "no mailbox connected" message, so a workflow referencing the built-in
  email_triage_reply skill still compiles and runs on a multi-org deployment.
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any, Dict

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from bestteam.tools.email_client import (
    _ImapBackend,
    email_draft_reply,
    email_find,
    email_read,
    make_email_tools,
)

from . import secret_store
from .db.email_credentials import get_email_credentials

_logger = logging.getLogger(__name__)

EMAIL_TOOL_NAMES = frozenset({"email_find", "email_read", "email_draft_reply"})


def spec_uses_email(
    db: Session,
    spec_raw: Dict[str, Any],
    org_id: int,
    *,
    workflow_version_id: int | None = None,
) -> bool:
    """True if any agent in a Specification resolves to an email tool.

    Email tools reach an agent either directly (its `tools:`) or via a skill it
    references (`skills:`, e.g. the built-in `email_triage_reply`), so this
    resolves each referenced skill to its tools. Used by the wizard to decide
    whether to ask the customer to connect a mailbox (and to gate deploy).
    """
    from .skills import load_skills

    skills = load_skills(
        db, org_id, workflow_version_id=workflow_version_id
    )
    for agent in spec_raw.get("agents", []) or []:
        names = set(agent.get("tools", []) or [])
        for skill_name in agent.get("skills", []) or []:
            skill = skills.get(skill_name)
            if skill is not None:
                names.update(skill.tools)
        if names & EMAIL_TOOL_NAMES:
            return True
    return False

_NOT_CONNECTED = (
    "No mailbox is connected for your team yet. Ask an admin to connect one "
    "before using the email tools."
)
_UNREADABLE = (
    "The connected mailbox can't be read right now (its stored credentials "
    "could not be decrypted). Ask an admin to reconnect it."
)


def _fixed_message_tools(message: str) -> Dict[str, Any]:
    """Three email tools that ignore their input and return `message`.

    Keeps the public names/docstrings so a workflow referencing the built-in
    email skill still compiles, while communicating the state to the model.
    """

    @functools.wraps(email_find)
    def find(query: str = "") -> str:
        return message

    @functools.wraps(email_read)
    def read(message_id: str) -> str:
        return message

    @functools.wraps(email_draft_reply)
    def draft_reply(message_id: str, body: str) -> str:
        return message

    return {"email_find": find, "email_read": read, "email_draft_reply": draft_reply}


def build_org_imap_backend(db: Session, org_id: int):
    """The org's IMAP backend from stored credentials, or None if unconnected.

    Decrypts the password; raises secret_store.SecretsKeyError / InvalidToken on
    a bad/rotated key (the caller decides how to surface that).
    """
    cred = get_email_credentials(db, org_id)
    if cred is None:
        return None
    password = secret_store.decrypt(cred.password_encrypted)
    return _ImapBackend(
        host=cred.host,
        user=cred.username,
        password=password,
        port=cred.port,
        drafts=cred.drafts_folder,
        restrict_to_public=True,  # customer-supplied host: validate + pin on connect
    )


def load_email_tools(db: Session, org_id: int) -> Dict[str, Any]:
    """Return the email tools for `org_id` (see module docstring for the order).

    Cheap: builds the backend object but opens no connection (IMAP connects
    lazily per operation), so this is safe to call on every workflow build.
    """
    cred = get_email_credentials(db, org_id)
    if cred is not None:
        try:
            backend = build_org_imap_backend(db, org_id)
        except (InvalidToken, secret_store.SecretsKeyError):
            # A wrong/rotated key must not crash the workflow build for this (or
            # any other) org -- surface it as a clear tool-level message and log
            # server-side. Startup validation normally catches this first.
            _logger.warning("Could not decrypt email credentials for org_id=%s", org_id)
            return _fixed_message_tools(_UNREADABLE)
        return make_email_tools(backend)
    if os.environ.get("BESTTEAM_EMAIL_BACKEND", "").strip():
        # Single-org / SDK env path handles email; don't shadow the env tools.
        return {}
    return _fixed_message_tools(_NOT_CONNECTED)
