"""`admin check-env`, the beta launch checklist as code (beta gate G6)."""

import pytest

pytestmark = pytest.mark.unit

pytest.importorskip("cryptography")

from ui.backend.env_check import check_environment, has_failures

_GOOD = {
    "BESTTEAM_SECRET_KEY": "a" * 64,
    "BESTTEAM_SECRETS_KEY": "b1PXm5jUu6qYqR2xM4Zc7L4b8dJ0Y5V2xQ3T1kH9m0E=",  # 32 bytes, url-safe b64
    "BESTTEAM_CORS_ORIGINS": "https://app.example.com",
    "VITE_API_BASE": "https://api.example.com",
    "VITE_WS_BASE": "wss://api.example.com",
    "BESTTEAM_RUN_RETENTION_DAYS": "90",
    "BESTTEAM_SENTRY_DSN": "https://k@o.ingest.sentry.io/1",
    "FORWARDED_ALLOW_IPS": "10.0.0.2",
}


def _by_name(findings):
    return {f.name: f for f in findings}


def test_a_complete_beta_environment_is_all_ok():
    findings = check_environment(_GOOD)
    assert not has_failures(findings)
    assert {f.level for f in findings} == {"OK"}, [f for f in findings if f.level != "OK"]


def test_the_env_example_copied_unchanged_fails_on_the_placeholder_secret():
    env = dict(_GOOD, BESTTEAM_SECRET_KEY="change-me-to-a-long-random-value")
    by = _by_name(check_environment(env))
    assert by["BESTTEAM_SECRET_KEY"].level == "FAIL"


def test_the_two_keys_must_differ_and_the_secrets_key_must_be_fernet():
    same = dict(_GOOD, BESTTEAM_SECRETS_KEY=_GOOD["BESTTEAM_SECRET_KEY"])
    assert _by_name(check_environment(same))["BESTTEAM_SECRETS_KEY"].level == "FAIL"
    not_fernet = dict(_GOOD, BESTTEAM_SECRETS_KEY="not-a-key")
    assert _by_name(check_environment(not_fernet))["BESTTEAM_SECRETS_KEY"].level == "FAIL"
    unset = dict(_GOOD)
    del unset["BESTTEAM_SECRETS_KEY"]
    assert _by_name(check_environment(unset))["BESTTEAM_SECRETS_KEY"].level == "WARN"


@pytest.mark.parametrize(
    "cors, level",
    [
        ("", "FAIL"),
        ("*", "FAIL"),
        ("https://app.example.com/", "FAIL"),
        ("app.example.com", "FAIL"),
        ("https://app.example.com,http://localhost:5173", "WARN"),
        ("https://app.example.com,https://other.example.com", "OK"),
    ],
)
def test_cors_origins_must_be_exact(cors, level):
    assert _by_name(check_environment(dict(_GOOD, BESTTEAM_CORS_ORIGINS=cors)))["BESTTEAM_CORS_ORIGINS"].level == level


def test_frontend_urls_are_required_and_should_be_tls():
    by = _by_name(check_environment(dict(_GOOD, VITE_API_BASE="", VITE_WS_BASE="ws://api.example.com")))
    assert by["VITE_API_BASE"].level == "FAIL"
    assert by["VITE_WS_BASE"].level == "WARN"
    assert _by_name(check_environment(dict(_GOOD, VITE_API_BASE="https://api.example.com/")))["VITE_API_BASE"].level == "FAIL"


def test_demo_pipelines_and_process_wide_mailbox_are_flagged():
    by = _by_name(check_environment(dict(_GOOD, BESTTEAM_DEMO_PIPELINES="1", BESTTEAM_EMAIL_HOST="imap.x")))
    assert by["BESTTEAM_DEMO_PIPELINES"].level == "FAIL"
    assert by["BESTTEAM_EMAIL_*"].level == "WARN" and "BESTTEAM_EMAIL_HOST" in by["BESTTEAM_EMAIL_*"].message
    assert _by_name(check_environment(dict(_GOOD, BESTTEAM_DEMO_PIPELINES="0")))["BESTTEAM_DEMO_PIPELINES"].level == "OK"


def test_beta_defaults_warn_when_left_at_dev_values():
    env = dict(_GOOD)
    for name in ("BESTTEAM_RUN_RETENTION_DAYS", "BESTTEAM_SENTRY_DSN", "FORWARDED_ALLOW_IPS"):
        del env[name]
    by = _by_name(check_environment(env))
    assert {by[n].level for n in ("BESTTEAM_RUN_RETENTION_DAYS", "BESTTEAM_SENTRY_DSN", "FORWARDED_ALLOW_IPS")} == {"WARN"}
    assert not has_failures(check_environment(env))
    assert _by_name(check_environment(dict(_GOOD, BESTTEAM_RUN_RETENTION_DAYS="ninety")))["BESTTEAM_RUN_RETENTION_DAYS"].level == "FAIL"


def test_a_malformed_sentry_dsn_fails_because_the_backend_would_not_start():
    pytest.importorskip("sentry_sdk")
    for dsn in ("garbage", "https://o.ingest.sentry.io/1", "https://k@o.ingest.sentry.io/"):
        finding = _by_name(check_environment(dict(_GOOD, BESTTEAM_SENTRY_DSN=dsn)))["BESTTEAM_SENTRY_DSN"]
        assert finding.level == "FAIL", (dsn, finding)
        assert "refuses to start" in finding.message


def test_a_dsn_without_the_sdk_is_a_warning(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # `import` raises ImportError
    monkeypatch.setitem(sys.modules, "sentry_sdk.utils", None)
    finding = _by_name(check_environment(_GOOD))["BESTTEAM_SENTRY_DSN"]
    assert finding.level == "WARN" and "not installed" in finding.message


def test_the_cli_prints_the_checklist_and_exits_1_on_a_failure(monkeypatch, capsys):
    pytest.importorskip("sqlalchemy")
    from ui.backend import admin

    for name in list(_GOOD) + ["BESTTEAM_DEMO_PIPELINES"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in _GOOD.items():
        monkeypatch.setenv(name, value)
    assert admin.main(["check-env"]) == 0
    out = capsys.readouterr().out
    assert "[OK]   BESTTEAM_CORS_ORIGINS" in out
    assert "no failures" in out

    monkeypatch.setenv("BESTTEAM_DEMO_PIPELINES", "yes")
    assert admin.main(["check-env"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] BESTTEAM_DEMO_PIPELINES" in out


def test_check_env_does_not_create_the_database(tmp_path):
    # `docker compose run --rm --no-deps backend python -m ui.backend.admin
    # check-env` is documented as safe on a box whose database does not
    # exist yet. A fresh interpreter, because `db_session` builds the
    # database at import and this process has long since imported it.
    import os
    import subprocess
    import sys

    pytest.importorskip("sqlalchemy")
    db_path = tmp_path / "data" / "bestteam.db"
    env = {k: v for k, v in os.environ.items() if not k.startswith("BESTTEAM_")}
    env.update(_GOOD, BESTTEAM_DB_PATH=str(db_path), BESTTEAM_MEMORY_DB="")
    proc = subprocess.run(
        [sys.executable, "-m", "ui.backend.admin", "check-env"],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no failures" in proc.stdout
    assert not db_path.exists(), "check-env created the database"
    assert not db_path.parent.exists()


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


# --- schema drift -------------------------------------------------------
#
# `init_db` runs `create_all`, which creates missing *tables* and never adds a
# column to a table that already exists. So a database left behind head boots
# clean and dies later, inside whichever feature touches the new column first
# (observed 2026-08-23: a dev database two revisions behind raised "no such
# column: knowledge_ingestion_jobs.chunk_size" from an ingestion run).


def _script_head():
    pytest.importorskip("alembic")
    from alembic.script import ScriptDirectory

    return ScriptDirectory(str(_repo_root() / "alembic")).get_current_head()


def _stamped_db(path, revision):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    if revision is not None:
        con.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    con.commit()
    con.close()


def test_a_database_stamped_at_head_is_ok(tmp_path):
    from ui.backend.env_check import check_schema

    db = tmp_path / "bestteam.db"
    _stamped_db(db, _script_head())
    finding = check_schema(db)
    assert finding.level == "OK", finding


def test_a_database_behind_head_fails_and_names_the_revisions(tmp_path):
    # The whole point: this is the FAIL that should have fired at launch
    # instead of a 500 from an ingestion run.
    from ui.backend.env_check import check_schema

    db = tmp_path / "bestteam.db"
    _stamped_db(db, "d2e3f4a5b6c7")  # the knowledge-ingestion-tables revision
    finding = check_schema(db)
    assert finding.level == "FAIL"
    assert "d2e3f4a5b6c7" in finding.message
    assert _script_head() in finding.message
    assert "alembic upgrade head" in finding.message


def test_a_database_that_does_not_exist_yet_is_not_a_failure(tmp_path):
    # check-env is documented as safe to run before the first start, and
    # `test_check_env_does_not_create_the_database` pins that it stays safe.
    from ui.backend.env_check import check_schema

    db = tmp_path / "data" / "bestteam.db"
    finding = check_schema(db)
    assert finding.level == "OK"
    assert not db.exists(), "check_schema created the database"
    assert not db.parent.exists()


def test_an_unstamped_create_all_database_warns(tmp_path):
    # `docs/deployment.md` says start the backend, then `alembic upgrade
    # head`. Between those two the schema is at head but carries no stamp;
    # it works, but the next migration has nothing to measure from.
    from ui.backend.env_check import check_schema

    db = tmp_path / "bestteam.db"
    import sqlite3

    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    finding = check_schema(db)
    assert finding.level == "WARN"
    assert "alembic upgrade head" in finding.message


def test_a_revision_the_code_does_not_know_fails(tmp_path):
    # A database from a newer checkout than the code being launched.
    from ui.backend.env_check import check_schema

    db = tmp_path / "bestteam.db"
    _stamped_db(db, "deadbeefcafe")
    finding = check_schema(db)
    assert finding.level == "FAIL"
    assert "deadbeefcafe" in finding.message


def test_the_cli_reports_schema_drift_and_exits_1(monkeypatch, capsys, tmp_path):
    pytest.importorskip("sqlalchemy")
    from ui.backend import admin

    db = tmp_path / "bestteam.db"
    _stamped_db(db, "d2e3f4a5b6c7")
    for name in list(_GOOD) + ["BESTTEAM_DEMO_PIPELINES"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in _GOOD.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BESTTEAM_DB_PATH", str(db))

    assert admin.main(["check-env"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] schema" in out
