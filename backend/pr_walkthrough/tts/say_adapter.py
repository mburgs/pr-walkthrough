"""SayTTSAdapter — last-resort fallback using macOS `say` + `afconvert`.

Zero Python dependencies; uses the macOS built-in TTS engine.
Produces an AIFF via `say`, converts to 22 050 Hz 16-bit mono WAV
via `afconvert`, then streams in 4096-sample chunks.

Only available on macOS (Darwin).
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import AsyncIterator

from ._wav import CHUNK_SAMPLES, build_wav_bytes

logger = logging.getLogger(__name__)

_DEFAULT_VOICE = "Samantha"  # built-in macOS neural voice


class SayTTSAdapter:
    """macOS `say` + `afconvert` TTS adapter."""

    def __init__(self, voice: str = _DEFAULT_VOICE) -> None:
        if not self.is_available():
            raise RuntimeError(
                "SayTTSAdapter requires macOS with `say` and `afconvert` in PATH."
            )
        self._default_voice = voice

    # ------------------------------------------------------------------
    # Protocol compliance
    # ------------------------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        return (
            platform.system() == "Darwin"
            and shutil.which("say") is not None
            and shutil.which("afconvert") is not None
        )

    def available_voices(self) -> list[str]:
        """Return voices from `say -v ?`."""
        try:
            result = subprocess.run(
                ["say", "-v", "?"],
                capture_output=True,
                text=True,
                check=True,
            )
            voices = []
            for line in result.stdout.splitlines():
                parts = line.split()
                if parts:
                    voices.append(parts[0])
            return ["default"] + voices
        except (subprocess.SubprocessError, OSError):
            return ["default", _DEFAULT_VOICE]

    async def synth(self, text: str, voice: str = "default") -> AsyncIterator[bytes]:
        return self._synth_iter(text, voice)

    async def _synth_iter(self, text: str, voice: str) -> AsyncIterator[bytes]:
        resolved_voice = self._default_voice if voice == "default" else voice
        wav_bytes = await asyncio.to_thread(self._say_to_wav, text, resolved_voice)
        async for chunk in _stream_wav_chunks(wav_bytes):
            yield chunk

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _say_to_wav(self, text: str, voice: str) -> bytes:
        """Shell out to `say` + `afconvert`, return WAV bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            aiff_path = Path(tmp) / "out.aiff"
            wav_path = Path(tmp) / "out.wav"

            # Step 1: synthesize to AIFF (say's native output format)
            subprocess.run(
                ["say", "-v", voice, "-o", str(aiff_path), text],
                check=True,
                capture_output=True,
            )

            # Step 2: convert to 22 050 Hz, 16-bit, mono WAV
            # afconvert flags:
            #   -f WAVE  → WAV container
            #   -d LEI16 → little-endian signed 16-bit integer
            #   -r 22050 → sample rate
            #   -c 1     → mono
            subprocess.run(
                [
                    "afconvert",
                    str(aiff_path),
                    str(wav_path),
                    "-f", "WAVE",
                    "-d", "LEI16@22050",
                    "-c", "1",
                ],
                check=True,
                capture_output=True,
            )

            return wav_path.read_bytes()


async def _stream_wav_chunks(wav_bytes: bytes) -> AsyncIterator[bytes]:
    """Yield the WAV in CHUNK_SAMPLES-sized pieces.

    The first yield contains the full RIFF header + first PCM slice so
    the consumer can begin playback immediately (contract requirement).
    Subsequent yields are raw PCM slices without a header.
    """
    import wave

    buf = __import__("io").BytesIO(wav_bytes)
    with wave.open(buf, "rb") as wf:
        header_size = buf.tell()  # after wave opens, position = end of header
        # Actually wave.open leaves the position at start of data;
        # re-read header manually.

    # Parse the WAV: header is the first 44 bytes (standard RIFF/WAV)
    # but let's read it properly.
    import io
    import struct

    bio = io.BytesIO(wav_bytes)
    # Read RIFF header
    riff, size, wave_id = struct.unpack("<4sI4s", bio.read(12))
    assert riff == b"RIFF" and wave_id == b"WAVE", "Not a valid WAV"

    # Find the data chunk
    header_end = 12
    data_start = None
    while True:
        chunk_id = bio.read(4)
        if not chunk_id:
            break
        (chunk_size,) = struct.unpack("<I", bio.read(4))
        if chunk_id == b"data":
            data_start = bio.tell()
            break
        bio.seek(chunk_size, 1)  # skip non-data chunks

    header_bytes = wav_bytes[:data_start]  # everything up to and including "data" chunk header

    # We want to re-yield a proper WAV for the first chunk.
    # The first chunk is: header_bytes + first PCM slice = a valid WAV.
    # But since we already have a complete WAV in wav_bytes (the entire file),
    # and the contract says "first chunk MUST be a valid WAV header",
    # the simplest approach is to yield the complete WAV as a single stream.
    # We then additionally yield fixed-size sub-chunks for true streaming.

    # Approach: yield a partial WAV file per slice
    # Each slice: emit a self-contained WAV (re-wrapped)
    CHUNK_BYTES = CHUNK_SAMPLES * 2  # 16-bit
    pcm = wav_bytes[data_start:]

    # First chunk: full WAV with first PCM slice
    first_pcm = pcm[:CHUNK_BYTES]
    yield build_wav_bytes(first_pcm)

    # Remaining chunks: raw PCM
    offset = CHUNK_BYTES
    while offset < len(pcm):
        yield pcm[offset : offset + CHUNK_BYTES]
        offset += CHUNK_BYTES
