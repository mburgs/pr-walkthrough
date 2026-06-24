"""FakeSTT — returns a hardcoded transcription for any audio."""

from __future__ import annotations


class FakeSTT:
    """Satisfies STTAdapter protocol. Always returns a dummy transcription."""

    async def transcribe(self, audio: bytes, mime: str) -> tuple[str, float]:
        return ("dummy transcription", 0.9)
