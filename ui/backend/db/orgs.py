"""Organization helpers: operator-provisioned customer orgs.

Orgs are created only via the `ui.backend.admin` operator CLI (there is no
self-service org creation). `seed_default_org` runs at backend bootstrap so
a fresh single-customer deployment has its one org from the start.
"""

from __future__ import annotations

import os
from typing import List, Optional

from sqlalchemy.orm import Session

from .models import Organization
from .validators import clean_identifier

DEFAULT_ORG_NAME = "default"


def ensure_email_single_org(db: Session, creating: int = 0) -> None:
    """Refuse process-wide email credentials on a multi-org deployment (CR-031).

    `BESTTEAM_EMAIL_*` configures exactly one mailbox for the *whole process*,
    and the built-in `email_triage_reply` skill is visible to every org -- so
    with more than one organization, any tenant's agents could read the
    configured customer's mailbox. This guards **only the env path**: the
    supported multi-tenant path is now per-org stored credentials
    (`db/email_credentials.py` + `email_tools.load_email_tools`), which resolve
    the running org's own mailbox and are *not* blocked here. `creating` counts
    orgs about to be inserted (the create-org CLI checks before it commits).

    Called at backend startup and from `create-org`; a no-op when
    `BESTTEAM_EMAIL_BACKEND` is unset.
    """
    if not os.environ.get("BESTTEAM_EMAIL_BACKEND", "").strip():
        return
    if db.query(Organization).count() + creating > 1:
        raise RuntimeError(
            "BESTTEAM_EMAIL_BACKEND is set but this deployment has more than one "
            "organization. Email credentials are process-wide (one mailbox shared "
            "by every org), so a multi-org deployment must not configure them -- "
            "unset the BESTTEAM_EMAIL_* variables, or keep a single organization. "
            "Per-org email credentials are a planned sub-project (see docs/STATUS.md)."
        )


def create_org(db: Session, name: str, display_name: str = "") -> Organization:
    name = clean_identifier(name, field="Organization name")
    if get_org_by_name(db, name) is not None:
        raise ValueError(f"Organization '{name}' already exists")
    org = Organization(name=name, display_name=display_name)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def get_org_by_name(db: Session, name: str) -> Optional[Organization]:
    return db.query(Organization).filter_by(name=name).one_or_none()


def set_org_active(db: Session, name: str, active: bool) -> Organization:
    """Activate/deactivate an org (reversible full suspend). Raises `ValueError`
    if the org is unknown. Idempotent -- setting the current value is a no-op."""
    org = get_org_by_name(db, name)
    if org is None:
        raise ValueError(f"Unknown organization '{name}'")
    org.active = active
    db.commit()
    db.refresh(org)
    return org


def get_or_create_org(db: Session, name: str, display_name: str = "") -> Organization:
    org = get_org_by_name(db, name)
    if org is not None:
        return org
    return create_org(db, name, display_name)


def list_orgs(db: Session) -> List[Organization]:
    return db.query(Organization).order_by(Organization.name).all()


def seed_default_org(db: Session) -> None:
    """Idempotent: ensure the 'default' org exists (single-customer deployments)."""
    get_or_create_org(db, DEFAULT_ORG_NAME, display_name="Default Organization")
