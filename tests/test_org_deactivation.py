"""Org deactivation (full suspend) -- data layer + enforcement.

See docs/superpowers/specs/2026-07-27-admin-org-user-management-design.md.
"""

from __future__ import annotations

import pytest

from ui.backend.db.database import init_db, make_engine, session_factory
from ui.backend.db.email_triggers import list_enabled_triggers, upsert_email_trigger
from ui.backend.db.orgs import create_org, get_org_by_name, set_org_active

pytestmark = pytest.mark.unit

def _session():
    engine = make_engine(":memory:")
    init_db(engine)
    return session_factory(engine)()


def test_new_org_is_active_by_default():
    db = _session()
    org = create_org(db, "acme")
    assert org.active is True


def test_set_org_active_toggles():
    db = _session()
    create_org(db, "acme")
    set_org_active(db, "acme", False)
    assert get_org_by_name(db, "acme").active is False
    set_org_active(db, "acme", True)
    assert get_org_by_name(db, "acme").active is True


def test_set_org_active_unknown_raises():
    db = _session()
    with pytest.raises(ValueError):
        set_org_active(db, "nope", False)


def test_list_enabled_triggers_excludes_inactive_org():
    db = _session()
    org = create_org(db, "acme")
    upsert_email_trigger(db, org.id, pipeline_name="wf", enabled=True, last_uid=0, uidvalidity=1)
    assert len(list_enabled_triggers(db)) == 1

    set_org_active(db, "acme", False)
    assert list_enabled_triggers(db) == []
