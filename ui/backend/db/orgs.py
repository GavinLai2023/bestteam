"""Organization helpers: operator-provisioned customer orgs.

Orgs are created only via the `ui.backend.admin` operator CLI (there is no
self-service org creation). `seed_default_org` runs at backend bootstrap so
a fresh single-customer deployment has its one org from the start.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .models import Organization

DEFAULT_ORG_NAME = "default"


def create_org(db: Session, name: str, display_name: str = "") -> Organization:
    if get_org_by_name(db, name) is not None:
        raise ValueError(f"Organization '{name}' already exists")
    org = Organization(name=name, display_name=display_name)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def get_org_by_name(db: Session, name: str) -> Optional[Organization]:
    return db.query(Organization).filter_by(name=name).one_or_none()


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
