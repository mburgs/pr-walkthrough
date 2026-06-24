"""Tests for WhisperSTTAdapter.

Includes:
- Keyword recognition test (uses the fixture WAV).
- Confidence range test.
- Conformance/compliance test: transcribe() MUST NOT open any network socket.
"""

from __future__ import annotations

import socket
import unittest.mock
from pathlib import Path

import pytest

FIXTURE_WAV = Path(__file__).parent / "fixtures" / "sample.wav"
# The fixture audio says: "hello world, this is a code review"
EXPECTED_KEYWORDS = {"hello", "world", "code", "review"}


@pytest.fixture()
def adapter():
    """Return a WhisperSTTAdapter with the tiny model for fast tests."""
    import os

    os.environ.setdefault("PR_WALKTHROUGH_WHISPER_MODEL", "tiny")
    from pr_walkthrough.stt.adapter import WhisperSTTAdapter

    return WhisperSTTAdapter(model_name="tiny")


@pytest.mark.asyncio
async def test_transcribe_keywords(adapter):
    """Transcription contains expected keywords from fixture audio."""
    audio_bytes = FIXTURE_WAV.read_bytes()
    text, confidence = await adapter.transcribe(audio_bytes, "audio/wav")

    assert isinstance(text, str), "text must be a string"
    assert len(text) > 0, "text must not be empty"

    text_lower = text.lower()
    matched = EXPECTED_KEYWORDS & set(text_lower.split())
    assert matched, (
        f"Expected at least one of {EXPECTED_KEYWORDS} in transcription, "
        f"got: {text!r}"
    )


@pytest.mark.asyncio
async def test_confidence_range(adapter):
    """Confidence is within [0, 1]."""
    audio_bytes = FIXTURE_WAV.read_bytes()
    _text, confidence = await adapter.transcribe(audio_bytes, "audio/wav")

    assert 0.0 <= confidence <= 1.0, f"confidence {confidence} not in [0, 1]"


@pytest.mark.asyncio
async def test_confidence_above_threshold(adapter):
    """Confidence exceeds 0.5 for clear speech."""
    audio_bytes = FIXTURE_WAV.read_bytes()
    _text, confidence = await adapter.transcribe(audio_bytes, "audio/wav")

    assert confidence > 0.5, (
        f"Expected confidence > 0.5 for clean TTS speech, got {confidence:.4f}"
    )


@pytest.mark.asyncio
async def test_no_network_calls(adapter):
    """Compliance: transcribe() MUST NOT open a socket (audio never leaves machine)."""
    audio_bytes = FIXTURE_WAV.read_bytes()

    original_socket = socket.socket
    opened_sockets: list[tuple] = []

    def patched_socket(*args, **kwargs):
        opened_sockets.append(args)
        return original_socket(*args, **kwargs)

    with unittest.mock.patch("socket.socket", side_effect=patched_socket):
        await adapter.transcribe(audio_bytes, "audio/wav")

    assert not opened_sockets, (
        f"transcribe() opened {len(opened_sockets)} socket(s) — "
        "audio must never leave the machine. "
        f"Sockets opened with args: {opened_sockets}"
    )


@pytest.mark.asyncio
async def test_tuple_return_type(adapter):
    """Return type conforms to the STTAdapter protocol: (str, float)."""
    audio_bytes = FIXTURE_WAV.read_bytes()
    result = await adapter.transcribe(audio_bytes, "audio/wav")

    assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
    assert len(result) == 2, f"Expected 2-tuple, got length {len(result)}"
    text, confidence = result
    assert isinstance(text, str), f"text must be str, got {type(text)}"
    assert isinstance(confidence, float), f"confidence must be float, got {type(confidence)}"
