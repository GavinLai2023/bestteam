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


def test_demo_workflows_and_process_wide_mailbox_are_flagged():
    by = _by_name(check_environment(dict(_GOOD, BESTTEAM_DEMO_WORKFLOWS="1", BESTTEAM_EMAIL_HOST="imap.x")))
    assert by["BESTTEAM_DEMO_WORKFLOWS"].level == "FAIL"
    assert by["BESTTEAM_EMAIL_*"].level == "WARN" and "BESTTEAM_EMAIL_HOST" in by["BESTTEAM_EMAIL_*"].message
    assert _by_name(check_environment(dict(_GOOD, BESTTEAM_DEMO_WORKFLOWS="0")))["BESTTEAM_DEMO_WORKFLOWS"].level == "OK"


def test_beta_defaults_warn_when_left_at_dev_values():
    env = dict(_GOOD)
    for name in ("BESTTEAM_RUN_RETENTION_DAYS", "BESTTEAM_SENTRY_DSN", "FORWARDED_ALLOW_IPS"):
        del env[name]
    by = _by_name(check_environment(env))
    assert {by[n].level for n in ("BESTTEAM_RUN_RETENTION_DAYS", "BESTTEAM_SENTRY_DSN", "FORWARDED_ALLOW_IPS")} == {"WARN"}
    assert not has_failures(check_environment(env))
    assert _by_name(check_environment(dict(_GOOD, BESTTEAM_RUN_RETENTION_DAYS="ninety")))["BESTTEAM_RUN_RETENTION_DAYS"].level == "FAIL"


def test_the_cli_prints_the_checklist_and_exits_1_on_a_failure(monkeypatch, capsys):
    pytest.importorskip("sqlalchemy")
    from ui.backend import admin

    for name in list(_GOOD) + ["BESTTEAM_DEMO_WORKFLOWS"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in _GOOD.items():
        monkeypatch.setenv(name, value)
    assert admin.main(["check-env"]) == 0
    out = capsys.readouterr().out
    assert "[OK]   BESTTEAM_CORS_ORIGINS" in out
    assert "no failures" in out

    monkeypatch.setenv("BESTTEAM_DEMO_WORKFLOWS", "yes")
    assert admin.main(["check-env"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] BESTTEAM_DEMO_WORKFLOWS" in out
