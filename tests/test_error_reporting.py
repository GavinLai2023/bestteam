"""The opt-in error-reporting channel (beta gate G4): off without a DSN,
initialised without content capture with one, and wired to exactly the
unhandled-request and failed-run paths."""

import sys
import types

import pytest

pytestmark = pytest.mark.integration

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from bestteam import AgentSpec, Specification, TeamSpec, PipelineSpec, validate_specification
from bestteam.core.trace import TraceEvent
from helpers import make_concurrent_safe_engine
from ui.backend import error_reporting
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import Run
from ui.backend.runtime import registry, run_in_background

_DSN = "https://key@example.ingest.sentry.io/1"


class _FakeSdk:
    def __init__(self):
        self.init_kwargs = None
        self.exceptions = []
        self.messages = []

    def init(self, **kwargs):
        self.init_kwargs = kwargs

    def capture_exception(self, exc, **scope):
        self.exceptions.append((exc, scope))

    def capture_message(self, message, **scope):
        self.messages.append((message, scope))


@pytest.fixture
def fake_sdk(monkeypatch):
    sdk = _FakeSdk()
    module = types.ModuleType("sentry_sdk")
    module.init = sdk.init
    module.capture_exception = sdk.capture_exception
    module.capture_message = sdk.capture_message
    dedupe = types.ModuleType("sentry_sdk.integrations.dedupe")
    dedupe.DedupeIntegration = lambda: "dedupe"
    monkeypatch.setitem(sys.modules, "sentry_sdk", module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", types.ModuleType("sentry_sdk.integrations"))
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.dedupe", dedupe)
    monkeypatch.setattr(error_reporting, "_sdk", None)
    return sdk


@pytest.fixture
def reporting_on(monkeypatch, fake_sdk):
    monkeypatch.setenv("BESTTEAM_SENTRY_DSN", _DSN)
    assert error_reporting.init_from_env() is True
    return fake_sdk


def test_off_without_a_dsn(monkeypatch, fake_sdk):
    monkeypatch.delenv("BESTTEAM_SENTRY_DSN", raising=False)
    assert error_reporting.init_from_env() is False
    assert error_reporting.is_enabled() is False
    error_reporting.report_exception(RuntimeError("x"), run_id="r1")
    error_reporting.report_message("m")
    assert fake_sdk.init_kwargs is None
    assert fake_sdk.exceptions == [] and fake_sdk.messages == []


def test_init_never_captures_content(monkeypatch, fake_sdk):
    monkeypatch.setenv("BESTTEAM_SENTRY_DSN", _DSN)
    monkeypatch.setenv("BESTTEAM_ENVIRONMENT", "beta")
    assert error_reporting.init_from_env() is True
    kwargs = fake_sdk.init_kwargs
    assert kwargs["dsn"] == _DSN
    assert kwargs["environment"] == "beta"
    assert kwargs["send_default_pii"] is False
    assert kwargs["max_request_body_size"] == "never"
    assert kwargs["include_local_variables"] is False
    assert kwargs["traces_sample_rate"] == 0.0
    assert kwargs["default_integrations"] is False
    assert kwargs["integrations"] == ["dedupe"]
    assert kwargs["before_send"] is error_reporting._scrub_event


def test_before_send_drops_exception_messages_but_keeps_type_and_stack():
    # A parser error quotes the model's output, an HTTP error the URL a tool
    # fetched: the message is the one part of an exception that can carry
    # customer content, so it never leaves the box.
    frame = {"filename": "ui/backend/runtime.py", "function": "_run", "lineno": 12}
    event = {
        "exception": {
            "values": [
                {"type": "OutputParserException", "value": "Could not parse: <the whole document>",
                 "stacktrace": {"frames": [frame]}},
                {"type": "ValueError", "value": "https://intranet.example/secret"},
            ]
        },
        "tags": {"run_id": "r1"},
    }
    scrubbed = error_reporting._scrub_event(event, hint=None)
    assert scrubbed["exception"]["values"] == [
        {"type": "OutputParserException", "stacktrace": {"frames": [frame]}},
        {"type": "ValueError"},
    ]
    assert scrubbed["tags"] == {"run_id": "r1"}
    # A message-only event has no exception block and passes through.
    assert error_reporting._scrub_event({"message": "Run failed: w"}, hint=None) == {"message": "Run failed: w"}


def test_reports_carry_string_tags_only(reporting_on):
    exc = RuntimeError("boom")
    error_reporting.report_exception(exc, run_id=42, pipeline="triage", org_id=None)
    error_reporting.report_message("Run failed: triage", run_id=42)
    assert reporting_on.exceptions == [(exc, {"tags": {"run_id": "42", "pipeline": "triage"}})]
    assert reporting_on.messages == [("Run failed: triage", {"level": "error", "tags": {"run_id": "42"}})]


def test_missing_sdk_is_a_warning_not_a_crash(monkeypatch, caplog):
    monkeypatch.setenv("BESTTEAM_SENTRY_DSN", _DSN)
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # `import` raises ImportError
    monkeypatch.setattr(error_reporting, "_sdk", None)
    with caplog.at_level("WARNING"):
        assert error_reporting.init_from_env() is False
    assert "sentry-sdk is not installed" in caplog.text


def test_a_reporting_failure_is_swallowed(reporting_on, monkeypatch):
    def explode(*_a, **_k):
        raise ConnectionError("sentry down")

    monkeypatch.setattr(sys.modules["sentry_sdk"], "capture_exception", explode)
    error_reporting.report_exception(RuntimeError("x"))  # must not raise


def test_a_blank_log_level_means_info():
    # `.env.example` ships `BESTTEAM_LOG_LEVEL=` blank and compose passes a
    # blank through, so main.py must not hand "" to `logging.basicConfig`
    # (`ValueError: Unknown level: ''` at import -- a restart loop). Read
    # from source: basicConfig runs once, at main's import.
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    example_lines = (root / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "BESTTEAM_LOG_LEVEL=" in example_lines
    source = (root / "ui" / "backend" / "main.py").read_text(encoding="utf-8")
    assert '(os.environ.get("BESTTEAM_LOG_LEVEL") or "INFO").upper()' in source
    assert 'get("BESTTEAM_LOG_LEVEL", "INFO")' not in source
    with pytest.raises(ValueError):
        import logging

        logging.getLogger("bestteam.test").setLevel("")  # the failure mode, for the record


# --- wiring: unhandled request exceptions ----------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from ui.backend import main as backend_main
    from ui.backend.db_session import get_db

    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    backend_main._pipeline_cache.clear()
    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    TestSessionLocal = session_factory(engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[get_db] = override_get_db
    with TestClient(backend_main.app, raise_server_exceptions=False) as c:
        yield c
    backend_main.app.dependency_overrides.clear()


def test_unhandled_request_exceptions_are_reported_with_the_route_template(client, reporting_on):
    from ui.backend import main as backend_main

    # A path parameter can be a capability token (`/api/share/{token}/...`),
    # so the report carries the route template, never the concrete path.
    @backend_main.app.get("/api/_test_boom/{token}")
    def boom(token: str):
        raise RuntimeError("kaboom")

    try:
        resp = client.get("/api/_test_boom/sekrit-capability-token")
    finally:
        backend_main.app.router.routes[:] = [
            r for r in backend_main.app.router.routes if getattr(r, "path", None) != "/api/_test_boom/{token}"
        ]
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}
    ((exc, scope),) = reporting_on.exceptions
    assert str(exc) == "kaboom"
    assert scope == {"tags": {"method": "GET", "path": "/api/_test_boom/{token}"}}
    assert "sekrit" not in repr(scope)


# --- wiring: failed runs ----------------------------------------------------


def _pipeline(tmp_path):
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:done")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    return validate_specification(spec, source=tmp_path / "w.yaml")


def _engine(tmp_path):
    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    return engine


def test_a_worker_thread_exception_is_reported_with_ids_not_content(tmp_path, reporting_on):
    engine = _engine(tmp_path)
    wf = _pipeline(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("model exploded")

    wf.stream = _boom
    run = registry.create("w", "the customer's private input")
    run_in_background(run.id, wf, "the customer's private input", engine=engine)

    ((exc, scope),) = reporting_on.exceptions
    assert str(exc) == "model exploded"
    assert scope == {"tags": {"run_id": run.id, "pipeline": "w"}}
    assert reporting_on.messages == []
    with session_factory(engine)() as s:
        assert s.get(Run, run.id).status == "failed"


def test_the_pipelines_own_run_failed_event_is_reported(tmp_path, reporting_on):
    engine = _engine(tmp_path)
    wf = _pipeline(tmp_path)

    def _fails_cleanly(*a, **k):
        yield TraceEvent(type="run_started", pipeline="w", data="")
        yield TraceEvent(type="run_failed", pipeline="w", data="Provider said no: " + "x" * 1000)

    wf.stream = _fails_cleanly
    run = registry.create("w", "in")
    run_in_background(run.id, wf, "in", engine=engine)

    assert reporting_on.exceptions == []
    ((message, scope),) = reporting_on.messages
    assert message == "Run failed: w"
    # Ids only: the reason is an exception's text and can quote a prompt or
    # a model's output. It stays on-box, in the run's persisted trace.
    assert scope == {"level": "error", "tags": {"run_id": run.id, "pipeline": "w"}}
    assert "Provider said no" not in repr(scope)
    with session_factory(engine)() as s:
        assert s.get(Run, run.id).status == "failed"
