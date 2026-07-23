"""run_in_background must not double-insert a runs row the caller pre-persisted."""

import pytest

pytest.importorskip("sqlalchemy")

from bestteam import AgentSpec, Specification, TeamSpec, WorkflowSpec, validate_specification
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import Run
from ui.backend.runtime import registry, run_in_background


def _engine():
    e = make_engine(":memory:")
    init_db(e)
    return e


def _workflow(tmp_path):
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:done")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        workflow=WorkflowSpec(steps=["t"]),
    )
    return validate_specification(spec, source=tmp_path / "w.yaml")


def test_reuses_preexisting_run_row_and_sets_terminal_status(tmp_path):
    engine = _engine()
    Session = session_factory(engine)
    # registry.create() is what publishes the in-memory Run the worker thread
    # publishes trace events against (see test_usage_metering.py); its id is
    # what a real caller would also use as the durable `runs` row's primary
    # key, so reuse it here rather than a disconnected literal.
    run = registry.create("w", "in", username="email-trigger")
    with Session() as s:
        s.add(Run(id=run.id, workflow="w", input="in", status="running", username="email-trigger"))
        s.commit()
    wf = _workflow(tmp_path)
    run_in_background(run.id, wf, "in", engine=engine, username="email-trigger")
    with Session() as s:
        rows = s.query(Run).filter_by(id=run.id).all()
        assert len(rows) == 1  # not double-inserted
        assert rows[0].status in ("completed", "failed")
        assert rows[0].username == "email-trigger"
