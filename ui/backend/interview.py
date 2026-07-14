"""Interview recording transcription API.

Accepts an audio/video file, transcribes it with OpenAI Whisper, then uses a
chat model to extract structured intent_text / as_is_text — the same two fields
the Team Builder wizard collects via typed input. Files larger than the Whisper
API's 25 MB limit are split into 10-minute MP3 chunks via ffmpeg (when
available) before transcription.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from bestteam.adapters.langgraph_adapter import _resolve_model
from bestteam.exceptions import BestTeamError

from .auth_api import get_current_user

router = APIRouter(
    prefix="/api/builder/interview",
    tags=["builder"],
    dependencies=[Depends(get_current_user)],
)

_WHISPER_FORMATS = {"mp3", "mp4", "m4a", "wav", "webm", "mpeg", "mpga"}
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # hard Whisper API limit per chunk
# Hard application-level ceiling on the whole upload, enforced before any
# buffering/transcription so a caller can't exhaust memory or ffmpeg/worker
# capacity by sending an arbitrarily large body -- even when ffmpeg is
# available to split it (CR-009).
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_SEGMENT_SECONDS = 600               # 10-min chunks; at 32 kbps ≈ 2.4 MB each
_FFMPEG_TIMEOUT_SECONDS = 300        # finite bound on each ffmpeg subprocess call

_EXTRACTION_SYSTEM_PROMPT = """\
You are a business analyst for bestteam, a multi-agent team-building platform.

You will receive a verbatim transcript of a consultant-customer interview. \
Extract two fields:

1. intent_text — What the customer wants help with. Write in first-person as \
the customer would say it ("We need...", "Our challenge is..."). Capture the \
core pain points, desired outcomes, and relevant context in 2-5 sentences. \
Use the customer's own words where possible.

2. as_is_text — How the customer handles this challenge today, described as \
their current manual or semi-automated process. If the interview does not \
discuss their current process, return an empty string.

Do not invent information not present in the transcript.\
"""


class InterviewExtraction(BaseModel):
    intent_text: str
    as_is_text: str


@functools.lru_cache(maxsize=1)
def _is_ffmpeg_available() -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # ffmpeg not installed / not on PATH, or hung -- treat as unavailable
        # rather than letting the exception escape as a 500.
        return False
    return result.returncode == 0


def _split_audio(data: bytes, ext: str, tmp_dir: str) -> list[str]:
    """Convert + split audio into ≤10-min MP3 chunks at 32 kbps via ffmpeg.

    Returns a sorted list of chunk file paths written inside `tmp_dir`.
    Raises `RuntimeError` if ffmpeg fails or produces no output.
    """
    input_path = os.path.join(tmp_dir, f"input.{ext}")
    chunk_pattern = os.path.join(tmp_dir, "chunk_%03d.mp3")
    with open(input_path, "wb") as f:
        f.write(data)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_path,
                "-vn",                          # drop video track (MP4/webm)
                "-ar", "16000",                 # 16 kHz — Whisper's native rate
                "-ac", "1",                     # mono
                "-b:a", "32k",                  # 32 kbps → ~2.4 MB per 10-min chunk
                "-f", "segment",
                "-segment_time", str(_SEGMENT_SECONDS),
                chunk_pattern,
            ],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg timed out while splitting the recording") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[:500]}")
    chunks = sorted(
        os.path.join(tmp_dir, name)
        for name in os.listdir(tmp_dir)
        if name.startswith("chunk_")
    )
    if not chunks:
        raise RuntimeError("ffmpeg produced no output chunks")
    return chunks


@router.post("/transcribe")
def transcribe_interview(
    file: UploadFile = File(...),
    model: str = Form(...),
) -> Dict[str, Any]:
    """Transcribe an interview recording and extract intent/as-is text.

    Returns `{transcript, intent_text, as_is_text}`. The `intent_text` and
    `as_is_text` fields are ready to pre-fill the Team Builder wizard's
    IntentPage; the raw `transcript` is included so the consultant can verify
    the extraction.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail=(
                "OPENAI_API_KEY is not configured. "
                "Audio transcription requires an OpenAI API key."
            ),
        )

    raw_name = (file.filename or "").lower()
    ext = raw_name.rsplit(".", 1)[-1] if "." in raw_name else ""
    if ext not in _WHISPER_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '.{ext}'. "
                f"Accepted formats: {', '.join(sorted(_WHISPER_FORMATS))}"
            ),
        )

    # Read at most the ceiling (+1 to detect overflow) so an oversized body is
    # rejected without buffering it all into memory (CR-009).
    data = file.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
        )

    if len(data) > _MAX_AUDIO_BYTES and not _is_ffmpeg_available():
        raise HTTPException(
            status_code=413,
            detail=(
                f"File exceeds 25 MB. Install ffmpeg on the server for automatic "
                "splitting of large recordings, or export your recording as MP3 "
                "at 64 kbps or lower."
            ),
        )

    # Resolve model early — fail fast before spending API budget on transcription.
    try:
        chat_model = _resolve_model(model)
    except BestTeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Model resolution failed: {exc}") from exc

    import openai  # deferred so the module loads without openai installed and patching is easy

    oa = openai.OpenAI()

    if len(data) <= _MAX_AUDIO_BYTES:
        # Small file: send directly to Whisper.
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            with open(tmp_path, "rb") as audio_file:
                response = oa.audio.transcriptions.create(model="whisper-1", file=audio_file)
            transcript = response.text
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Transcription failed: {exc}") from exc
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    else:
        # Large file: split into chunks, transcribe each, join.
        tmp_dir = tempfile.mkdtemp()
        try:
            try:
                chunks = _split_audio(data, ext, tmp_dir)
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            parts: list[str] = []
            for chunk_path in chunks:
                try:
                    with open(chunk_path, "rb") as audio_file:
                        response = oa.audio.transcriptions.create(model="whisper-1", file=audio_file)
                    parts.append(response.text)
                except Exception as exc:
                    raise HTTPException(
                        status_code=502, detail=f"Transcription failed on chunk: {exc}"
                    ) from exc
            transcript = "\n".join(parts)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # LLM extraction — intentionally broad except: the transcript is always
    # returned even if extraction fails, so the consultant can still proceed.
    try:
        structured = chat_model.with_structured_output(InterviewExtraction)
        result = structured.invoke(
            [
                SystemMessage(content=_EXTRACTION_SYSTEM_PROMPT),
                HumanMessage(content=f"Interview transcript:\n\n{transcript}"),
            ]
        )
        if not isinstance(result, InterviewExtraction):
            result = InterviewExtraction.model_validate(result)
        intent_text = result.intent_text
        as_is_text = result.as_is_text
    except Exception:
        intent_text = transcript
        as_is_text = ""

    return {"transcript": transcript, "intent_text": intent_text, "as_is_text": as_is_text}
