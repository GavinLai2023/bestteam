"""Per-org pre-LLM filter settings (email automation Phase 4a).

Row CRUD only. The rules themselves live in `ui/backend/email_filter.py` so
they can be tested without a database -- the same split `retention.py` uses.

Nothing here commits: callers own the transaction boundary.
"""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy.orm import Session

from ..email_filter import FilterSettings
from .models import OrgEmailFilterSetting


def get_filter_row(db: Session, org_id: int) -> Optional[OrgEmailFilterSetting]:
    return (
        db.query(OrgEmailFilterSetting)
        .filter(OrgEmailFilterSetting.org_id == org_id)
        .one_or_none()
    )


def _clean(values) -> tuple:
    """Drop blanks and duplicates while keeping the admin's order, so a list
    they can read back is the list they typed."""
    seen, out = set(), []
    for value in values or []:
        text = str(value).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return tuple(out)


def get_filter_settings(db: Session, org_id: int) -> FilterSettings:
    """This org's rules, or the defaults when it has no row (bulk filtered)."""
    row = get_filter_row(db, org_id)
    if row is None:
        return FilterSettings()
    return FilterSettings(
        skip_bulk=bool(row.skip_bulk),
        sender_blocklist=_clean(row.sender_blocklist),
        sender_allowlist=_clean(row.sender_allowlist),
        subject_blocklist=_clean(row.subject_blocklist),
    )


def set_filter_settings(
    db: Session,
    org_id: int,
    *,
    skip_bulk: bool,
    sender_blocklist: Sequence,
    sender_allowlist: Sequence,
    subject_blocklist: Sequence,
) -> OrgEmailFilterSetting:
    row = get_filter_row(db, org_id)
    if row is None:
        row = OrgEmailFilterSetting(org_id=org_id)
        db.add(row)
    row.skip_bulk = bool(skip_bulk)
    row.sender_blocklist = list(_clean(sender_blocklist))
    row.sender_allowlist = list(_clean(sender_allowlist))
    row.subject_blocklist = list(_clean(subject_blocklist))
    db.flush()
    return row
