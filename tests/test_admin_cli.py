"""Tests for the operator admin CLI (`python -m ui.backend.admin`)."""

import pytest

pytest.importorskip("sqlalchemy")

from ui.backend import admin as admin_cli
from ui.backend.db import init_db, make_engine, session_factory
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
