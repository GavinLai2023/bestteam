"""The backend half of share-chat token streaming.

See docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md. The
sink is what turns the SDK's per-delta callback into WebSocket-sized
`reply_delta` events, so these tests are about coalescing and the reset
sentinel -- not about the model loop that produces the deltas
(tests/test_streaming.py covers that).
"""

import pytest

from bestteam.adapters.langgraph_adapter import STREAM_RESET
from ui.backend.runtime import _TokenSink

pytestmark = pytest.mark.unit


@pytest.fixture
def published(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        "ui.backend.runtime.registry.publish_transient",
        lambda run_id, event: events.append(event),
    )
    return events


def test_small_deltas_are_coalesced_into_one_event(published):
    sink = _TokenSink("run-1")
    for character in "hello":
        sink(character)
    assert published == [], "five characters is well under the flush threshold"

    sink.flush()

    assert published == [{"type": "reply_delta", "data": "hello"}]


def test_a_long_delta_run_flushes_at_the_character_threshold(published):
    sink = _TokenSink("run-1")
    for _ in range(50):
        sink("x")

    assert len(published) == 1
    assert len(published[0]["data"]) >= 40


def test_the_reset_sentinel_drops_the_buffer_and_publishes_a_reset(published):
    sink = _TokenSink("run-1")
    sink("partial")
    sink(STREAM_RESET)
    sink.flush()

    assert published == [{"type": "reply_reset", "data": None}]


def test_flush_on_an_empty_buffer_publishes_nothing(published):
    _TokenSink("run-1").flush()
    assert published == []


@pytest.mark.integration
def test_a_share_run_publishes_its_reply_as_deltas(tmp_path, monkeypatch):
    """End to end through the real worker: SDK sink -> coalescer -> registry."""
    from bestteam import AgentSpec, PipelineSpec, Specification, TeamSpec, validate_specification
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import Run
    from ui.backend.runtime import registry, run_in_background

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:Hello, colleague.")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    pipeline = validate_specification(spec, source=tmp_path / "w.yaml")

    transient: list[dict] = []
    monkeypatch.setattr(
        registry, "publish_transient", lambda run_id, event: transient.append(event)
    )

    run = registry.create("w", "in", username="share-link")
    with Session() as session:
        session.add(
            Run(
                id=run.id,
                pipeline="w",
                input="in",
                status="running",
                username="share-link",
                trigger_context={"share_link_id": 1, "share_session_id": 1, "turn_number": 1},
            )
        )
        session.commit()

    run_in_background(run.id, pipeline, "in", engine=engine, username="share-link")

    assert "".join(e["data"] for e in transient if e["type"] == "reply_delta") == "Hello, colleague."
    # The durable path is untouched: no delta reached the replay log, and the
    # authoritative answer still arrives as run_completed.
    assert all(e["type"] != "reply_delta" for e in registry.get(run.id).events)
    assert registry.get(run.id).events[-1]["data"] == "Hello, colleague."


@pytest.mark.integration
def test_a_non_share_run_gets_no_sink(tmp_path, monkeypatch):
    from bestteam import AgentSpec, PipelineSpec, Specification, TeamSpec, validate_specification
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db
    from ui.backend.runtime import registry, run_in_background

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:Hello.")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    pipeline = validate_specification(spec, source=tmp_path / "w.yaml")

    transient: list[dict] = []
    monkeypatch.setattr(
        registry, "publish_transient", lambda run_id, event: transient.append(event)
    )

    run = registry.create("w", "in")
    run_in_background(run.id, pipeline, "in", engine=engine)

    assert transient == []
