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
import os
from typing import Any, Dict

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

_NOT_CONNECTED = (
    "No mailbox is connected for your team yet. Ask an admin to connect one "
    "before using the email tools."
)


def _not_connected_tools() -> Dict[str, Any]:
    @functools.wraps(email_find)
    def find(query: str = "") -> str:
        return _NOT_CONNECTED

    @functools.wraps(email_read)
    def read(message_id: str) -> str:
        return _NOT_CONNECTED

    @functools.wraps(email_draft_reply)
    def draft_reply(message_id: str, body: str) -> str:
        return _NOT_CONNECTED

    return {"email_find": find, "email_read": read, "email_draft_reply": draft_reply}


def load_email_tools(db: Session, org_id: int) -> Dict[str, Any]:
    """Return the email tools for `org_id` (see module docstring for the order).

    Cheap: builds the backend object but opens no connection (IMAP connects
    lazily per operation), so this is safe to call on every workflow build.
    """
    cred = get_email_credentials(db, org_id)
    if cred is not None:
        password = secret_store.decrypt(cred.password_encrypted)
        backend = _ImapBackend(
            host=cred.host,
            user=cred.username,
            password=password,
            port=cred.port,
            drafts=cred.drafts_folder,
        )
        return make_email_tools(backend)
    if os.environ.get("BESTTEAM_EMAIL_BACKEND", "").strip():
        # Single-org / SDK env path handles email; don't shadow the env tools.
        return {}
    return _not_connected_tools()
