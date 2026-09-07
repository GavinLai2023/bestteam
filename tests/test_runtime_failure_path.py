"""A run whose row could not be written up front still ends with exactly one
`runs` row and a `run_failed` trace event that points at it (spec §5). The
three 2026-09-05 orphans in the dev database were this path."""

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")

from sqlalchemy import event

from bestteam import AgentSpec, PipelineSpec, Specification, TeamSpec, validate_specification
from helpers import make_test_engine
from ui.backend.db import init_db, session_factory
from ui.backend.db.models import Run, TraceEventRecord
from ui.backend.runtime import registry, run_in_background


def _pipeline(tmp_path):
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:done")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    return validate_specification(spec, source=tmp_path / "w.yaml")


def test_terminal_trace_is_never_written_without_its_run_row(tmp_path):
    engine = make_test_engine(tmp_path)  # foreign keys enforced
    init_db(engine)
    state = {"tripped": False}

    @event.listens_for(engine, "before_cursor_execute")
    def _fail_the_first_read_of_the_runs_row(conn, cursor, statement, parameters, context, executemany):
        # The worker could not read/write its `runs` row and then still had
        # to publish a terminal event. Only the first SELECT on `runs` fails.
        if not state["tripped"] and statement.lstrip().upper().startswith("SELECT") and "runs" in statement:
            state["tripped"] = True
            raise RuntimeError("simulated: runs row unreadable")

    run = registry.create("w", "in")
    run_in_background(run.id, _pipeline(tmp_path), "in", engine=engine)

    assert state["tripped"]
    try:
        with session_factory(engine)() as db:
            rows = db.query(Run).filter_by(id=run.id).all()
            assert [r.status for r in rows] == ["failed"]
            events = db.query(TraceEventRecord).filter_by(run_id=run.id).order_by(TraceEventRecord.seq).all()
            assert [e.type for e in events] == ["run_failed"]
    finally:
        engine.dispose()
