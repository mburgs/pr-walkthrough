"""KokoroTTSAdapter — preferred local TTS via hexgrad/kokoro (Apache-2.0).

Kokoro 82M runs on CPU and produces high-quality English speech.

First use downloads ~300 MB of model weights from HuggingFace.
Set HF_TOKEN env var to avoid rate-limiting during the download.

Output: 24 kHz float32 → resampled to 22 050 Hz, 16-bit mono WAV chunks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from ._wav import (
    CHUNK_SAMPLES,
    TARGET_SAMPLE_RATE,
    build_wav_bytes,
    float32_to_pcm16,
    resample_pcm16,
)

logger = logging.getLogger(__name__)

# Kokoro native output rate
_KOKORO_RATE = 24000

_DEFAULT_VOICE = "af_heart"

# All voices bundled with the kokoro package
_KOKORO_VOICES = [
    "af_heart",
    "af_bella",
    "af_nicole",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_michael",
    "bf_emma",
    "bf_isabella",
    "bm_george",
    "bm_lewis",
]


class KokoroTTSAdapter:
    """TTS adapter using Kokoro 82M.

    Voice names map to Kokoro voice IDs.  Pass voice='default' to use
    the package default (af_heart).
    """

    def __init__(self, voice: str = _DEFAULT_VOICE) -> None:
        from kokoro import KPipeline  # type: ignore[import-untyped]

        logger.info("Loading Kokoro pipeline (this may download weights on first run)…")
        self._pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
        self._default_voice = voice

    # ------------------------------------------------------------------
    # Protocol compliance
    # ------------------------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        try:
            import kokoro  # noqa: F401
            import torch  # noqa: F401

            return True
        except ImportError:
            return False

    def available_voices(self) -> list[str]:
        return ["default"] + _KOKORO_VOICES

    async def synth(self, text: str, voice: str = "default") -> AsyncIterator[bytes]:
        """Yield WAV chunks at 22 050 Hz, 16-bit mono.

        The first yielded chunk is a complete WAV file (header + PCM) so the
        consumer can start playback immediately even though more chunks follow.
        For kokoro, each sentence segment is yielded as its own chunk.
        """
        return self._synth_iter(text, voice)

    async def _synth_iter(self, text: str, voice: str) -> AsyncIterator[bytes]:
        resolved_voice = _DEFAULT_VOICE if voice == "default" else voice

        def _run_kokoro() -> list[bytes]:
            """Run the synchronous kokoro pipeline and collect PCM chunks."""
            chunks: list[bytes] = []
            for result in self._pipeline(text, voice=resolved_voice):
                audio_tensor = result.output.audio  # float32 tensor
                pcm16 = float32_to_pcm16(audio_tensor.numpy())
                pcm22 = resample_pcm16(pcm16, _KOKORO_RATE, TARGET_SAMPLE_RATE)
                chunks.append(pcm22)
            return chunks

        pcm_chunks = await asyncio.to_thread(_run_kokoro)

        if not pcm_chunks:
            return

        # Yield the first chunk as a complete WAV (header + PCM).
        # Subsequent chunks are raw PCM that can be appended.
        # Consumers that need a single WAV can concatenate all chunks.
        # To keep the protocol simple (first chunk = valid WAV), we yield
        # every chunk as a full standalone WAV.  The consumer concatenating
        # them for a single playback should strip headers from chunks 2+,
        # OR collect all raw PCM then wrap once.  The orchestrator (stream 2)
        # streams them as independent HTTP chunks; the browser assembles one
        # WAV from the stream.
        #
        # Simplest correct approach for streaming: yield each segment as a
        # full WAV.  First chunk satisfies "must start with valid RIFF header".

        for pcm in pcm_chunks:
            wav = build_wav_bytes(pcm)
            yield wav

    # ------------------------------------------------------------------
    # Internal streaming in CHUNK_SAMPLES-sized pieces for long text
    # ------------------------------------------------------------------

    async def synth_chunked(
        self, text: str, voice: str = "default"
    ) -> AsyncIterator[bytes]:
        """Like synth() but yields fixed-size sample chunks (4096 samples each).

        Gives finer-grained streaming for long narrations.
        """
        import numpy as np

        resolved_voice = _DEFAULT_VOICE if voice == "default" else voice

        def _run_kokoro() -> bytes:
            all_pcm = b""
            for result in self._pipeline(text, voice=resolved_voice):
                pcm16 = float32_to_pcm16(result.output.audio.numpy())
                pcm22 = resample_pcm16(pcm16, _KOKORO_RATE, TARGET_SAMPLE_RATE)
                all_pcm += pcm22
            return all_pcm

        all_pcm = await asyncio.to_thread(_run_kokoro)
        async for chunk in _stream_pcm(all_pcm):
            yield chunk


async def _stream_pcm(pcm: bytes) -> AsyncIterator[bytes]:
    """Yield a complete WAV first, then fixed-size raw PCM chunks."""
    from ._wav import SAMPLE_WIDTH, build_wav_bytes

    chunk_bytes = CHUNK_SAMPLES * SAMPLE_WIDTH

    # First chunk: full WAV containing first slice of PCM
    first_slice = pcm[:chunk_bytes]
    yield build_wav_bytes(first_slice)

    # Subsequent chunks: raw PCM (no header)
    offset = chunk_bytes
    while offset < len(pcm):
        yield pcm[offset : offset + chunk_bytes]
        offset += chunk_bytes
