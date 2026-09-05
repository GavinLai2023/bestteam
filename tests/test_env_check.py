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
    "BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL": "openai:text-embedding-3-small",
    "BESTTEAM_KB_DEFAULT_RERANK_MODEL": "cross-encoder:BAAI/bge-reranker-base",
    "TAVILY_API_KEY": "tvly-not-a-real-key",
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


def test_an_unset_kb_embedding_default_warns_that_customers_get_keyword_search_only():
    env = dict(_GOOD)
    del env["BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL"]
    by = _by_name(check_environment(env))
    assert by["BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL"].level == "WARN"
    assert "keyword" in by["BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL"].message
    # The rerank default is only meaningful once an embedding model is set, so
    # it must not add a second warning about a KB feature that cannot apply.
    assert "BESTTEAM_KB_DEFAULT_RERANK_MODEL" not in by
    assert not has_failures(check_environment(env))


def test_an_unset_tavily_key_warns_that_web_search_degrades_silently():
    env = dict(_GOOD)
    del env["TAVILY_API_KEY"]
    by = _by_name(check_environment(env))
    assert by["TAVILY_API_KEY"].level == "WARN"
    assert "web_search" in by["TAVILY_API_KEY"].message
    # A deployment with no research team has no reason to be blocked on it.
    assert not has_failures(check_environment(env))


def test_a_fake_kb_embedding_default_fails_because_retrieval_would_be_noise():
    env = dict(_GOOD, BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL="fake:32")
    finding = _by_name(check_environment(env))["BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL"]
    assert finding.level == "FAIL"
    assert has_failures(check_environment(env))


def test_an_unset_rerank_default_warns_only_once_semantic_search_is_on():
    env = dict(_GOOD)
    del env["BESTTEAM_KB_DEFAULT_RERANK_MODEL"]
    finding = _by_name(check_environment(env))["BESTTEAM_KB_DEFAULT_RERANK_MODEL"]
    assert finding.level == "WARN"
    assert "BAAI/bge-reranker-base" in finding.message
    assert not has_failures(check_environment(env))


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


# --- org retention -------------------------------------------------------
#
# BESTTEAM_RUN_RETENTION_DAYS only sets the default for orgs created *after*
# it -- an existing org with no retention period keeps run history forever
# and the env WARN alone never mentions it. `check_org_retention` reads the
# live database (read-only, like check_schema) and names those orgs.


def _org_db(path, orgs, retention=()):
    """orgs: list of names; retention: (org_index_1_based, days) pairs."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE organizations (id INTEGER PRIMARY KEY, name TEXT, active INTEGER DEFAULT 1)")
    con.execute("CREATE TABLE org_retention_settings (id INTEGER PRIMARY KEY, org_id INTEGER, run_retention_days INTEGER)")
    for name in orgs:
        con.execute("INSERT INTO organizations (name) VALUES (?)", (name,))
    for org_id, days in retention:
        con.execute(
            "INSERT INTO org_retention_settings (org_id, run_retention_days) VALUES (?, ?)",
            (org_id, days),
        )
    con.commit()
    con.close()


def test_org_retention_missing_database_is_ok(tmp_path):
    from ui.backend.env_check import check_org_retention

    db = tmp_path / "data" / "bestteam.db"
    finding = check_org_retention(db)
    assert finding.level == "OK"
    assert not db.exists(), "check_org_retention created the database"


def test_org_retention_all_orgs_covered_is_ok(tmp_path):
    from ui.backend.env_check import check_org_retention

    db = tmp_path / "bestteam.db"
    _org_db(db, ["default", "acme"], retention=[(1, 90), (2, 30)])
    finding = check_org_retention(db)
    assert finding.level == "OK", finding


def test_org_retention_warns_and_names_uncovered_orgs(tmp_path):
    from ui.backend.env_check import check_org_retention

    db = tmp_path / "bestteam.db"
    # acme has no settings row at all; globex has a row with NULL days
    # (policy switched off) -- both keep history forever.
    _org_db(db, ["default", "acme", "globex"], retention=[(1, 90), (3, None)])
    finding = check_org_retention(db)
    assert finding.level == "WARN"
    assert "acme" in finding.message
    assert "globex" in finding.message
    assert "default" not in finding.message


def test_org_retention_pre_migration_schema_is_ok(tmp_path):
    # A database without the organizations table (pre-migration, or built by
    # an old checkout): nothing to report, and never a crash.
    import sqlite3

    from ui.backend.env_check import check_org_retention

    db = tmp_path / "bestteam.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    finding = check_org_retention(db)
    assert finding.level == "OK"


def test_the_cli_includes_the_org_retention_finding(monkeypatch, capsys, tmp_path):
    pytest.importorskip("sqlalchemy")
    from ui.backend import admin

    db = tmp_path / "bestteam.db"
    _stamped_db(db, _script_head())
    import sqlite3

    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE organizations (id INTEGER PRIMARY KEY, name TEXT, active INTEGER DEFAULT 1)")
    con.execute("CREATE TABLE org_retention_settings (id INTEGER PRIMARY KEY, org_id INTEGER, run_retention_days INTEGER)")
    con.execute("INSERT INTO organizations (name) VALUES ('beta-org')")
    con.commit()
    con.close()
    for name in list(_GOOD) + ["BESTTEAM_DEMO_PIPELINES"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in _GOOD.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BESTTEAM_DB_PATH", str(db))

    admin.main(["check-env"])
    out = capsys.readouterr().out
    assert "[WARN] org-retention" in out
    assert "beta-org" in out


def _catalog_db(path, rows):
    """A database holding just a model_catalog table with the given
    (spec, tier) rows -- the columns check_model_catalog reads."""
    import sqlite3

    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE model_catalog (id INTEGER PRIMARY KEY, spec TEXT, tier TEXT)")
    con.executemany("INSERT INTO model_catalog (spec, tier) VALUES (?, ?)", rows)
    con.commit()
    con.close()


def test_model_catalog_missing_database_is_ok(tmp_path):
    from ui.backend.env_check import check_model_catalog

    db = tmp_path / "data" / "bestteam.db"
    finding = check_model_catalog(db)
    assert finding.level == "OK"
    assert not db.exists(), "check_model_catalog created the database"


def test_model_catalog_with_a_real_chat_model_is_ok(tmp_path):
    from ui.backend.env_check import check_model_catalog

    db = tmp_path / "bestteam.db"
    _catalog_db(db, [("fake:ok", "fast"), ("openai:gpt-4o", "advanced")])
    finding = check_model_catalog(db)
    assert finding.level == "OK", finding


def test_model_catalog_of_only_fakes_warns(tmp_path):
    # Exactly the shape tests/e2e/conftest.py::_reshape_model_catalog leaves
    # behind if it ever runs against a real database: every provider entry
    # deleted, fake-architect:e2e added. The wizard then silently builds the
    # same canned team for every intent.
    from ui.backend.env_check import check_model_catalog

    db = tmp_path / "bestteam.db"
    _catalog_db(db, [("fake-architect:e2e", "fast"), ("fake:ok", "fast")])
    finding = check_model_catalog(db)
    assert finding.level == "WARN"
    assert "fake-architect:e2e" in finding.message


def test_model_catalog_empty_warns(tmp_path):
    from ui.backend.env_check import check_model_catalog

    db = tmp_path / "bestteam.db"
    _catalog_db(db, [])
    finding = check_model_catalog(db)
    assert finding.level == "WARN"


def test_model_catalog_of_only_embedding_models_warns(tmp_path):
    # Embedding entries share this table but can never be an agent's model
    # (see db/model_catalog.py::list_chat_entries), so a catalog holding
    # nothing else still leaves the wizard with no model to build with.
    from ui.backend.env_check import check_model_catalog

    db = tmp_path / "bestteam.db"
    _catalog_db(db, [("openai:text-embedding-3-small", "embedding")])
    finding = check_model_catalog(db)
    assert finding.level == "WARN"


def test_model_catalog_pre_migration_schema_is_ok(tmp_path):
    import sqlite3

    from ui.backend.env_check import check_model_catalog

    db = tmp_path / "bestteam.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    finding = check_model_catalog(db)
    assert finding.level == "OK"


def test_the_cli_includes_the_model_catalog_finding(monkeypatch, capsys, tmp_path):
    pytest.importorskip("sqlalchemy")
    from ui.backend import admin

    db = tmp_path / "bestteam.db"
    _stamped_db(db, _script_head())
    _catalog_db(db, [("fake-architect:e2e", "fast")])
    monkeypatch.setattr("sys.argv", ["admin", "check-env"])
    for key, value in _GOOD.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BESTTEAM_DB_PATH", str(db))
    admin.main()
    assert "model-catalog" in capsys.readouterr().out
