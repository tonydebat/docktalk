"""Whisper transcription + browser User-Agent → MIME inference.

The audio recorder widget returns raw bytes only — no MIME type. The browser
container format is platform-deterministic (iOS Safari = M4A/AAC, everything
else = WebM/Opus), so we infer it from the rider's User-Agent header.

This module has no Streamlit dependency; the caller passes in the headers
mapping (e.g. ``st.context.headers``).
"""

from __future__ import annotations

import io
import os
from typing import Mapping

WHISPER_MODEL = "whisper-1"
WHISPER_TIMEOUT_SECONDS = 30.0

_MIME_WEBM = "audio/webm"
_MIME_M4A = "audio/mp4"

_EXTENSION_BY_MIME = {
    _MIME_M4A: "m4a",
    _MIME_WEBM: "webm",
}


def infer_audio_mime_from_headers(headers: Mapping[str, str] | None) -> str:
    """Return the MIME type the rider's browser will have produced.

    iPhone and iPad use the native MediaRecorder, which outputs M4A/AAC.
    Every other browser we care about outputs WebM/Opus. iPadOS 13+ identifies
    its Safari as desktop Mac in the User-Agent; we treat the explicit iPad
    keyword as authoritative, but fall back to WebM for "Macintosh" only —
    a real Mac speaks WebM.

    Missing or empty User-Agent → defaults to WebM. Whisper accepts both
    natively, so picking the wrong one only matters for edge-case rejections,
    not the common path.
    """
    if not headers:
        return _MIME_WEBM

    user_agent = ""
    for key, value in headers.items():
        if key.lower() == "user-agent":
            user_agent = value or ""
            break

    if not user_agent:
        return _MIME_WEBM

    ua = user_agent.lower()
    if "iphone" in ua or "ipad" in ua or "ipod" in ua:
        return _MIME_M4A

    return _MIME_WEBM


def transcribe_audio(
    audio_bytes: bytes,
    mime_type: str,
    *,
    client=None,
) -> str:
    """Transcribe an audio blob via the OpenAI Whisper API.

    Args:
        audio_bytes: Raw audio bytes from the browser recorder.
        mime_type: The container MIME type (``audio/mp4`` or ``audio/webm``).
            Determines the file-tuple extension Whisper sees.
        client: Optional pre-built OpenAI client. The caller can inject a fake
            for testing. Defaults to ``openai.OpenAI()`` which reads
            ``OPENAI_API_KEY`` from the environment.

    Returns:
        The transcript string. May be empty if the audio is silent.
    """
    if not audio_bytes:
        return ""

    if client is None:
        from openai import OpenAI

        client = OpenAI(timeout=WHISPER_TIMEOUT_SECONDS)

    extension = _EXTENSION_BY_MIME.get(mime_type, "webm")
    file_tuple = (f"audio.{extension}", io.BytesIO(audio_bytes), mime_type)

    response = client.audio.transcriptions.create(
        model=WHISPER_MODEL,
        file=file_tuple,
    )

    text = getattr(response, "text", "")
    return (text or "").strip()
