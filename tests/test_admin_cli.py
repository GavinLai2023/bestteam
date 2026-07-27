"""Tests for the operator admin CLI (`python -m ui.backend.admin`)."""

import pytest

pytest.importorskip("sqlalchemy")

from ui.backend import admin as admin_cli
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.orgs import create_org, get_org_by_name
from ui.backend.db.users import create_user, get_user_by_username


@pytest.fixture
def session_local(monkeypatch):
    # In-memory StaticPool engine shared across sessions; patch the CLI's factory.
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    monkeypatch.setattr(admin_cli, "SessionLocal", Session)
    return Session


def test_promote_and_demote(session_local, capsys):
    with session_local() as db:
        create_user(db, "alice", "pw")

    assert admin_cli.main(["promote", "alice"]) == 0
    with session_local() as db:
        assert get_user_by_username(db, "alice").is_admin is True
    assert "now an admin" in capsys.readouterr().out

    assert admin_cli.main(["demote", "alice"]) == 0
    with session_local() as db:
        assert get_user_by_username(db, "alice").is_admin is False


def test_list_shows_only_admins(session_local, capsys):
    with session_local() as db:
        create_user(db, "alice", "pw")
        create_user(db, "bob", "pw")
    admin_cli.main(["promote", "alice"])
    capsys.readouterr()  # discard prior output

    admin_cli.main(["list"])
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" not in out


def test_promote_unknown_user_errors(session_local):
    # argparse.error() exits with SystemExit(2).
    with pytest.raises(SystemExit):
        admin_cli.main(["promote", "ghost"])


def test_promote_org_member_errors(session_local, monkeypatch):
    # CR-030: admin is platform-wide, so org members can't be promoted --
    # the operator creates a separate org-less account (create-user --platform).
    admin_cli.main(["create-org", "acme"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        create_user(db, "alice", "pw", org_id=org.id)

    with pytest.raises(SystemExit):
        admin_cli.main(["promote", "alice"])
    with session_local() as db:
        assert get_user_by_username(db, "alice").is_admin is False


# ---------------------------------------------------------------------------
# Org provisioning (create-org / list-orgs / create-user)
# ---------------------------------------------------------------------------

def _patch_password(monkeypatch, password="pw"):
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda prompt="": password)


def test_create_org_and_list_orgs(session_local, capsys):
    assert admin_cli.main(["create-org", "acme", "--display-name", "Acme Corp"]) == 0
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        assert org is not None
        assert org.display_name == "Acme Corp"

    capsys.readouterr()
    admin_cli.main(["list-orgs"])
    out = capsys.readouterr().out
    assert "acme (Acme Corp)" in out


def test_create_duplicate_org_errors(session_local):
    admin_cli.main(["create-org", "acme"])
    with pytest.raises(SystemExit):
        admin_cli.main(["create-org", "acme"])


def test_create_second_org_errors_when_email_configured(session_local, monkeypatch):
    # CR-031: process-wide email credentials + more than one org would expose
    # the configured mailbox to every tenant, so create-org refuses.
    admin_cli.main(["create-org", "acme"])
    monkeypatch.setenv("BESTTEAM_EMAIL_BACKEND", "imap")

    with pytest.raises(SystemExit):
        admin_cli.main(["create-org", "globex"])
    with session_local() as db:
        assert get_org_by_name(db, "globex") is None


def test_create_user_in_org(session_local, monkeypatch):
    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch)

    assert admin_cli.main(["create-user", "alice", "--org", "acme"]) == 0
    with session_local() as db:
        user = get_user_by_username(db, "alice")
        assert user is not None
        assert user.org_id == get_org_by_name(db, "acme").id
        assert user.is_admin is False


def test_create_user_defaults_to_default_org(session_local, monkeypatch):
    with session_local() as db:
        create_org(db, "default")
    _patch_password(monkeypatch)

    admin_cli.main(["create-user", "bob"])
    with session_local() as db:
        assert get_user_by_username(db, "bob").org_id == get_org_by_name(db, "default").id


def test_create_platform_user_has_no_org(session_local, monkeypatch):
    _patch_password(monkeypatch)
    admin_cli.main(["create-user", "op", "--platform"])
    with session_local() as db:
        assert get_user_by_username(db, "op").org_id is None


def test_create_user_unknown_org_errors(session_local, monkeypatch):
    _patch_password(monkeypatch)
    with pytest.raises(SystemExit):
        admin_cli.main(["create-user", "alice", "--org", "ghost"])


def test_create_user_password_mismatch_errors(session_local, monkeypatch):
    admin_cli.main(["create-org", "acme"])
    answers = iter(["pw1", "pw2"])
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda prompt="": next(answers))
    with pytest.raises(SystemExit):
        admin_cli.main(["create-user", "alice", "--org", "acme"])


# ---------------------------------------------------------------------------
# Recovery: delete-user / move-user
# ---------------------------------------------------------------------------

def test_delete_user_removes_account(session_local, monkeypatch, capsys):
    _patch_password(monkeypatch)
    admin_cli.main(["create-user", "op", "--platform"])
    capsys.readouterr()
    assert admin_cli.main(["delete-user", "op"]) == 0
    assert "Deleted user 'op'" in capsys.readouterr().out
    with session_local() as db:
        assert get_user_by_username(db, "op") is None


def test_delete_unknown_user_errors(session_local):
    with pytest.raises(SystemExit):
        admin_cli.main(["delete-user", "ghost"])


def test_delete_user_purges_memory(session_local, monkeypatch, tmp_path, capsys):
    # Review r2 #2: account deletion clears the user's memory before releasing
    # the username, so a recreated same-named account can't recall it.
    from bestteam import SqliteBM25Memory
    from bestteam.core.memory import EPISODIC

    mem_path = tmp_path / "mem.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(mem_path))
    _patch_password(monkeypatch)
    admin_cli.main(["create-user", "alice", "--platform"])

    store = SqliteBM25Memory(str(mem_path))
    store.add("alice", EPISODIC, "alice's secret", org_id=1)
    store.close()

    capsys.readouterr()
    assert admin_cli.main(["delete-user", "alice"]) == 0
    assert "Purged 1 memory record" in capsys.readouterr().out

    store = SqliteBM25Memory(str(mem_path))
    assert store.all("alice", org_id=None) == []
    store.close()


def test_delete_user_fails_closed_when_memory_purge_fails(session_local, monkeypatch):
    # If the memory purge fails, the account must NOT be deleted (username stays
    # claimed, so nothing leaks).
    _patch_password(monkeypatch)
    admin_cli.main(["create-user", "op", "--platform"])

    def boom(username):
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(admin_cli, "_purge_user_memory", boom)

    with pytest.raises(SystemExit):
        admin_cli.main(["delete-user", "op"])
    with session_local() as db:
        assert get_user_by_username(db, "op") is not None


def test_delete_user_warns_when_memory_not_configured(session_local, monkeypatch, capsys):
    # Review r3 #1: unset BESTTEAM_MEMORY_DB -> account still deleted, but a loud
    # warning (not a silent "clean") so the operator knows memory wasn't purged.
    monkeypatch.delenv("BESTTEAM_MEMORY_DB", raising=False)
    _patch_password(monkeypatch)
    admin_cli.main(["create-user", "op", "--platform"])
    capsys.readouterr()

    assert admin_cli.main(["delete-user", "op"]) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "no per-user memory was purged".lower() in out.lower()
    with session_local() as db:
        assert get_user_by_username(db, "op") is None


def test_delete_user_does_not_create_missing_memory_db(session_local, monkeypatch, tmp_path):
    # Review r3 #1: a configured-but-absent store path must not be created (which
    # would give a false "purged, all clean").
    missing = tmp_path / "nope.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(missing))
    _patch_password(monkeypatch)
    admin_cli.main(["create-user", "op", "--platform"])

    assert admin_cli.main(["delete-user", "op"]) == 0
    assert not missing.exists()


def test_delete_unknown_user_does_not_purge_memory(session_local, monkeypatch, tmp_path):
    # Review r3 #4: validate the account BEFORE purging, so deleting an unknown
    # username can't remove orphaned memory for that name.
    from bestteam import SqliteBM25Memory

    mem_path = tmp_path / "mem.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(mem_path))
    store = SqliteBM25Memory(str(mem_path))
    store.add("ghost", "episodic", "orphaned secret", org_id=1)
    store.close()

    with pytest.raises(SystemExit):
        admin_cli.main(["delete-user", "ghost"])  # no such user

    store = SqliteBM25Memory(str(mem_path))
    assert len(store.all("ghost", org_id=None)) == 1  # orphan memory untouched
    store.close()


def test_move_user_reconciles_legacy_memory_to_source_org(session_local, monkeypatch, tmp_path):
    # Review r3 #3: moving a user first binds their legacy NULL-org rows to the
    # source org, so pre-SP-2 data stays attributable to the org it came from.
    from bestteam import SqliteBM25Memory

    _patch_password(monkeypatch)
    admin_cli.main(["create-org", "acme"])
    admin_cli.main(["create-org", "beta"])
    admin_cli.main(["create-user", "alice", "--org", "acme"])
    with session_local() as db:
        acme_id = get_org_by_name(db, "acme").id

    mem_path = tmp_path / "mem.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(mem_path))
    store = SqliteBM25Memory(str(mem_path))
    store.add("alice", "episodic", "legacy note")  # org_id NULL
    store.close()

    assert admin_cli.main(["move-user", "alice", "--to-org", "beta"]) == 0

    store = SqliteBM25Memory(str(mem_path))
    # The legacy row is now bound to acme (the source org), not left NULL.
    assert [r.content for r in store.all("alice", org_id=acme_id)] == ["legacy note"]
    assert store.all("alice", org_id=None) == store.all("alice", org_id=acme_id)
    store.close()


def test_move_user_to_platform(session_local, monkeypatch):
    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch)
    admin_cli.main(["create-user", "alice", "--org", "acme"])
    assert admin_cli.main(["move-user", "alice", "--platform"]) == 0
    with session_local() as db:
        assert get_user_by_username(db, "alice").org_id is None


def test_move_user_into_full_org_errors(session_local, monkeypatch):
    # Recovery still respects one-member-per-org: moving into an org that
    # already has a member is refused.
    admin_cli.main(["create-org", "acme"])
    admin_cli.main(["create-org", "beta"])
    _patch_password(monkeypatch)
    admin_cli.main(["create-user", "alice", "--org", "acme"])
    admin_cli.main(["create-user", "bob", "--org", "beta"])
    with pytest.raises(SystemExit):
        admin_cli.main(["move-user", "alice", "--to-org", "beta"])
    with session_local() as db:
        assert get_user_by_username(db, "alice").org_id == get_org_by_name(db, "acme").id


# ---------------------------------------------------------------------------
# Per-org email (set-email / clear-email)
# ---------------------------------------------------------------------------

@pytest.fixture
def secrets_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())


def test_set_email_stores_encrypted_credentials(session_local, secrets_key, monkeypatch):
    from ui.backend import secret_store
    from ui.backend.db.email_credentials import get_email_credentials

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch, "app-password")

    assert admin_cli.main(
        ["set-email", "acme", "--host", "imap.gmail.com", "--user", "support@acme.com"]
    ) == 0
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        cred = get_email_credentials(db, org.id)
        assert cred is not None
        assert cred.host == "imap.gmail.com"
        assert cred.username == "support@acme.com"
        assert cred.password_encrypted != "app-password"  # not plaintext
        assert secret_store.decrypt(cred.password_encrypted) == "app-password"


def test_set_email_unknown_org_errors(session_local, secrets_key, monkeypatch):
    _patch_password(monkeypatch)
    with pytest.raises(SystemExit):
        admin_cli.main(["set-email", "ghost", "--host", "h", "--user", "u"])


def test_clear_email_removes_credentials(session_local, secrets_key, monkeypatch, capsys):
    from ui.backend.db.email_credentials import get_email_credentials

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch)
    admin_cli.main(["set-email", "acme", "--host", "h", "--user", "u"])
    capsys.readouterr()

    assert admin_cli.main(["clear-email", "acme"]) == 0
    assert "Disconnected" in capsys.readouterr().out
    with session_local() as db:
        assert get_email_credentials(db, get_org_by_name(db, "acme").id) is None


def test_clear_email_recovers_when_key_cannot_decrypt(session_local, secrets_key, monkeypatch):
    # Finding 1: if the secrets key is lost/rotated the app refuses to start,
    # but the operator CLI must still run so an admin can recover. clear-email
    # deletes the row without decrypting, and the CLI never triggers the
    # app-startup credential guard (it doesn't import main).
    from cryptography.fernet import Fernet
    from ui.backend.db.email_credentials import get_email_credentials

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch)
    admin_cli.main(["set-email", "acme", "--host", "h", "--user", "u"])
    # Key changes to something that can't decrypt the stored row.
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())

    assert admin_cli.main(["clear-email", "acme"]) == 0
    with session_local() as db:
        assert get_email_credentials(db, get_org_by_name(db, "acme").id) is None


def test_set_email_test_flag_rejects_bad_login(session_local, secrets_key, monkeypatch):
    from bestteam.exceptions import ConfigurationError
    from ui.backend.db.email_credentials import get_email_credentials

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch)
    # Make the login test fail; credentials must not be saved.
    monkeypatch.setattr(
        "bestteam.tools.email_client._ImapBackend._connect",
        lambda self: (_ for _ in ()).throw(ConfigurationError("bad login")),
    )
    with pytest.raises(SystemExit):
        admin_cli.main(["set-email", "acme", "--host", "h", "--user", "u", "--test"])
    with session_local() as db:
        assert get_email_credentials(db, get_org_by_name(db, "acme").id) is None


# ---------------------------------------------------------------------------
# Trigger disable on mailbox change (mirrors org_settings.py's coverage,
# applied to the operator CLI path -- finding #1's CLI-parity gap)
# ---------------------------------------------------------------------------

def test_set_email_disables_trigger_on_host_change(session_local, secrets_key, monkeypatch):
    from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger
    from ui.backend.db.orgs import get_org_by_name

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch)
    admin_cli.main(["set-email", "acme", "--host", "imap.acme.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True,
                             last_uid=10, uidvalidity=1)

    admin_cli.main(["set-email", "acme", "--host", "imap.other.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        assert get_email_trigger(db, org.id).enabled is False


def test_set_email_keeps_trigger_enabled_on_password_only_rotation(session_local, secrets_key, monkeypatch):
    from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger
    from ui.backend.db.orgs import get_org_by_name

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch, "old-pw")
    admin_cli.main(["set-email", "acme", "--host", "imap.acme.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True,
                             last_uid=10, uidvalidity=1)

    _patch_password(monkeypatch, "new-pw")
    admin_cli.main(["set-email", "acme", "--host", "imap.acme.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        assert get_email_trigger(db, org.id).enabled is True


def test_clear_email_disables_trigger(session_local, secrets_key, monkeypatch):
    from ui.backend.db.email_triggers import get_email_trigger, upsert_email_trigger
    from ui.backend.db.orgs import get_org_by_name

    admin_cli.main(["create-org", "acme"])
    _patch_password(monkeypatch)
    admin_cli.main(["set-email", "acme", "--host", "imap.acme.com", "--user", "u@acme.com"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True,
                             last_uid=10, uidvalidity=1)

    admin_cli.main(["clear-email", "acme"])
    with session_local() as db:
        org = get_org_by_name(db, "acme")
        assert get_email_trigger(db, org.id).enabled is False


# ---------------------------------------------------------------------------
# Org activation (activate-org / deactivate-org)
# ---------------------------------------------------------------------------

def test_deactivate_and_activate_org(session_local):
    admin_cli.main(["create-org", "acme"])
    assert admin_cli.main(["deactivate-org", "acme"]) == 0
    with session_local() as db:
        assert get_org_by_name(db, "acme").active is False
    assert admin_cli.main(["activate-org", "acme"]) == 0
    with session_local() as db:
        assert get_org_by_name(db, "acme").active is True


def test_deactivate_unknown_org_errors(session_local):
    with pytest.raises(SystemExit):
        admin_cli.main(["deactivate-org", "ghost"])


# --- F4: db helpers reject blank identities/passwords (CLI path) ---

def test_create_user_rejects_blank(session_local):
    from ui.backend.db.users import create_user
    with session_local() as db:
        with pytest.raises(ValueError):
            create_user(db, "   ", "pw")
        with pytest.raises(ValueError):
            create_user(db, "bob", "   ")


def test_create_org_rejects_blank(session_local):
    from ui.backend.db.orgs import create_org
    with session_local() as db:
        with pytest.raises(ValueError):
            create_org(db, "   ")
