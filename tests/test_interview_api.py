"""Tests for the interview transcription API (ui/backend/interview.py)."""

import asyncio

import pytest
from unittest.mock import MagicMock, patch

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("openai")  # these tests patch openai.OpenAI; skip when the optional dep is absent

from fastapi.testclient import TestClient

from helpers import create_user_and_login
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db_session import get_db
from ui.backend.interview import InterviewExtraction


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    engine = make_engine(":memory:")
    init_db(engine)
    TestSessionLocal = session_factory(engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[get_db] = override_get_db
    try:
        test_client = TestClient(backend_main.app)
        token = create_user_and_login(test_client)
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _make_mock_llm(extraction: InterviewExtraction) -> MagicMock:
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value.invoke.return_value = extraction
    return mock_llm


def _make_whisper_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    return resp


_SMALL_MP3 = b"\x00" * 100  # well under 25 MB


def test_transcribe_returns_all_three_fields(client):
    transcript = "Consultant: What's slowing you down? Customer: Email replies take all day."
    extraction = InterviewExtraction(
        intent_text="We need help handling customer email replies.",
        as_is_text="One person manually reads and replies to all emails every day.",
    )
    mock_llm = _make_mock_llm(extraction)

    with patch("openai.OpenAI") as mock_cls, \
         patch("ui.backend.interview._resolve_model", return_value=mock_llm):
        mock_cls.return_value.audio.transcriptions.create.return_value = _make_whisper_response(transcript)
        resp = client.post(
            "/api/builder/interview/transcribe",
            data={"model": "fake:hello"},
            files={"file": ("interview.mp3", _SMALL_MP3, "audio/mpeg")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["transcript"] == transcript
    assert body["intent_text"] == extraction.intent_text
    assert body["as_is_text"] == extraction.as_is_text


def test_unsupported_file_extension_returns_400(client):
    resp = client.post(
        "/api/builder/interview/transcribe",
        data={"model": "fake:hello"},
        files={"file": ("notes.pdf", b"pdf content", "application/pdf")},
    )
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.json()["detail"]


def test_file_with_no_extension_returns_400(client):
    resp = client.post(
        "/api/builder/interview/transcribe",
        data={"model": "fake:hello"},
        files={"file": ("recording", b"\x00" * 100, "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.json()["detail"]


def test_file_over_25mb_without_ffmpeg_returns_413(client):
    large_data = b"\x00" * (25 * 1024 * 1024 + 1)
    with patch("ui.backend.interview._is_ffmpeg_available", return_value=False):
        resp = client.post(
            "/api/builder/interview/transcribe",
            data={"model": "fake:hello"},
            files={"file": ("long_meeting.mp3", large_data, "audio/mpeg")},
        )
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert "ffmpeg" in detail
    assert "64 kbps" in detail


def test_file_over_25mb_with_ffmpeg_transcribes_chunks(client):
    large_data = b"\x00" * (25 * 1024 * 1024 + 1)
    chunk_paths = ["/fake/chunk_000.mp3", "/fake/chunk_001.mp3"]
    transcripts = ["First half of the interview.", "Second half of the interview."]
    extraction = InterviewExtraction(intent_text="We need a bot.", as_is_text="Manual today.")
    mock_llm = _make_mock_llm(extraction)

    whisper_responses = [_make_whisper_response(t) for t in transcripts]

    with patch("ui.backend.interview._is_ffmpeg_available", return_value=True), \
         patch("ui.backend.interview._split_audio", return_value=chunk_paths), \
         patch("builtins.open", create=True) as mock_open, \
         patch("openai.OpenAI") as mock_cls, \
         patch("ui.backend.interview._resolve_model", return_value=mock_llm):
        mock_cls.return_value.audio.transcriptions.create.side_effect = whisper_responses
        mock_open.return_value.__enter__ = lambda s: MagicMock()
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        resp = client.post(
            "/api/builder/interview/transcribe",
            data={"model": "fake:hello"},
            files={"file": ("long.mp3", large_data, "audio/mpeg")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert transcripts[0] in body["transcript"]
    assert transcripts[1] in body["transcript"]


def test_missing_api_key_returns_503(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resp = client.post(
        "/api/builder/interview/transcribe",
        data={"model": "fake:hello"},
        files={"file": ("interview.mp3", _SMALL_MP3, "audio/mpeg")},
    )
    assert resp.status_code == 503
    assert "OPENAI_API_KEY" in resp.json()["detail"]


def test_whisper_failure_returns_502(client):
    with patch("openai.OpenAI") as mock_cls, \
         patch("ui.backend.interview._resolve_model", return_value=MagicMock()):
        mock_cls.return_value.audio.transcriptions.create.side_effect = RuntimeError("quota exceeded")
        resp = client.post(
            "/api/builder/interview/transcribe",
            data={"model": "fake:hello"},
            files={"file": ("interview.mp3", _SMALL_MP3, "audio/mpeg")},
        )
    assert resp.status_code == 502
    assert "Transcription failed" in resp.json()["detail"]


def test_bad_model_spec_returns_400(client):
    from bestteam.exceptions import BestTeamError

    with patch("ui.backend.interview._resolve_model", side_effect=BestTeamError("Unknown model 'bad:model'")):
        resp = client.post(
            "/api/builder/interview/transcribe",
            data={"model": "bad:model"},
            files={"file": ("interview.mp3", _SMALL_MP3, "audio/mpeg")},
        )
    assert resp.status_code == 400
    assert "Unknown model" in resp.json()["detail"]


def test_extraction_failure_falls_back_to_raw_transcript(client):
    transcript = "Raw interview text that couldn't be structured."
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value.invoke.side_effect = RuntimeError("LLM error")

    with patch("openai.OpenAI") as mock_cls, \
         patch("ui.backend.interview._resolve_model", return_value=mock_llm):
        mock_cls.return_value.audio.transcriptions.create.return_value = _make_whisper_response(transcript)
        resp = client.post(
            "/api/builder/interview/transcribe",
            data={"model": "fake:hello"},
            files={"file": ("interview.mp3", _SMALL_MP3, "audio/mpeg")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["intent_text"] == transcript
    assert body["as_is_text"] == ""


def test_no_auth_header_returns_401(client):
    resp = client.post(
        "/api/builder/interview/transcribe",
        data={"model": "fake:hello"},
        files={"file": ("interview.mp3", _SMALL_MP3, "audio/mpeg")},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401


def test_oversized_upload_returns_413(client, monkeypatch):
    # CR-009: a body over the hard app-level ceiling is rejected before any
    # transcription/ffmpeg work, even when ffmpeg is available for splitting.
    from ui.backend import interview

    monkeypatch.setattr(interview, "_MAX_UPLOAD_BYTES", 50)
    with patch("ui.backend.interview._is_ffmpeg_available", return_value=True):
        resp = client.post(
            "/api/builder/interview/transcribe",
            data={"model": "fake:hello"},
            files={"file": ("huge.mp3", b"\x00" * 200, "audio/mpeg")},
        )
    assert resp.status_code == 413
    assert "limit" in resp.json()["detail"]


def test_oversized_content_length_rejected_before_body_is_parsed(client, monkeypatch):
    # CR-009: an over-ceiling upload is rejected by the body-size middleware --
    # which runs before FastAPI parses/spools the multipart body (request.form()
    # is called before dependency resolution). Proof it is pre-parse: OPENAI_API_KEY
    # is unset, yet we get 413 (the middleware) rather than the handler's 503.
    from ui.backend import interview

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(interview, "_MAX_UPLOAD_BYTES", 50)
    resp = client.post(
        "/api/builder/interview/transcribe",
        data={"model": "fake:hello"},
        files={"file": ("huge.mp3", b"\x00" * 500, "audio/mpeg")},
    )
    assert resp.status_code == 413
    assert "limit" in resp.json()["detail"]


def test_chunked_oversized_body_is_rejected_before_the_downstream_app(monkeypatch):
    # CR-009: no Content-Length (or an understated one) must not let multipart
    # parsing/spooling consume an unbounded body. Exercise the ASGI receive
    # wrapper directly with two streamed chunks, as normal HTTP clients fill in
    # Content-Length automatically.
    from ui.backend import interview
    from ui.backend.main import _RequestBodyLimitMiddleware

    monkeypatch.setattr(interview, "_MAX_UPLOAD_BYTES", 50)
    chunks = iter(
        [
            {"type": "http.request", "body": b"a" * 30, "more_body": True},
            {"type": "http.request", "body": b"b" * 30, "more_body": False},
        ]
    )
    sent = []

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body", False):
                return

    scope = {"type": "http", "path": "/api/builder/interview/transcribe", "headers": []}
    asyncio.run(_RequestBodyLimitMiddleware(downstream)(scope, receive, send))

    assert sent[0]["status"] == 413


def test_is_ffmpeg_available_false_when_binary_missing():
    # CR-009: a missing ffmpeg binary must read as "unavailable", not raise.
    from ui.backend import interview

    interview._is_ffmpeg_available.cache_clear()
    with patch("ui.backend.interview.subprocess.run", side_effect=FileNotFoundError()):
        assert interview._is_ffmpeg_available() is False
    interview._is_ffmpeg_available.cache_clear()


def test_split_audio_raises_on_ffmpeg_timeout(tmp_path):
    # CR-009: the split subprocess has a finite timeout; a hang surfaces as a
    # RuntimeError (-> 502) rather than blocking a worker forever.
    from ui.backend import interview

    with patch(
        "ui.backend.interview.subprocess.run",
        side_effect=interview.subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1),
    ):
        with pytest.raises(RuntimeError, match="timed out"):
            interview._split_audio(b"\x00" * 10, "mp3", str(tmp_path))


def test_ffmpeg_failure_returns_502(client):
    large_data = b"\x00" * (25 * 1024 * 1024 + 1)

    with patch("ui.backend.interview._is_ffmpeg_available", return_value=True), \
         patch("ui.backend.interview._split_audio", side_effect=RuntimeError("ffmpeg failed: ...")), \
         patch("ui.backend.interview._resolve_model", return_value=MagicMock()):
        resp = client.post(
            "/api/builder/interview/transcribe",
            data={"model": "fake:hello"},
            files={"file": ("long.mp3", large_data, "audio/mpeg")},
        )
    assert resp.status_code == 502
