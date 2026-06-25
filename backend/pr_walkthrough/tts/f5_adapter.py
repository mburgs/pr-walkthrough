"""F5TTSAdapter — F5-TTS implementation of TTSAdapter.

F5-TTS is voice-cloning by reference. We bundle its own example English
reference voice as the default; PR_WALKTHROUGH_F5_REF can override.
Heavyweight: ~1GB of weights, 5-10s per segment on CPU. Lazy-loaded.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import AsyncIterator

from ._wav import (
    TARGET_SAMPLE_RATE, build_wav_bytes, float32_to_pcm16, resample_pcm16,
)

logger = logging.getLogger(__name__)

# F5-TTS native sample rate
_F5_RATE = 24000

# The reference text matches the bundled basic_ref_en.wav
_DEFAULT_REF_TEXT = "Some call me nature, others call me mother nature."


class F5TTSAdapter:
    """F5-TTS adapter using the bundled English reference voice."""

    def __init__(self) -> None:
        if not self.is_available():
            raise RuntimeError("F5TTSAdapter: f5-tts not installed")
        import os
        from f5_tts.api import F5TTS  # type: ignore[import-untyped]

        logger.info("Loading F5-TTS (~1GB download on first run)…")
        self._model = F5TTS(model="F5TTS_v1_Base")
        self._ref_file = os.environ.get(
            "PR_WALKTHROUGH_F5_REF", self._bundled_reference()
        )
        self._ref_text = os.environ.get(
            "PR_WALKTHROUGH_F5_REF_TEXT", _DEFAULT_REF_TEXT
        )

    @staticmethod
    def _bundled_reference() -> str:
        import importlib.util as _u
        spec = _u.find_spec("f5_tts")
        if spec and spec.submodule_search_locations:
            return str(
                Path(spec.submodule_search_locations[0])
                / "infer" / "examples" / "basic" / "basic_ref_en.wav"
            )
        raise RuntimeError("f5_tts bundled reference not found")

    @classmethod
    def is_available(cls) -> bool:
        import importlib.util as _u
        return _u.find_spec("f5_tts") is not None

    def available_voices(self) -> list[str]:
        return ["default"]

    def synth(self, text: str, voice: str = "default") -> AsyncIterator[bytes]:
        return self._synth_iter(text)

    async def _synth_iter(self, text: str) -> AsyncIterator[bytes]:
        pcm = await asyncio.to_thread(self._tts_to_pcm16, text)
        yield build_wav_bytes(pcm)

    def _tts_to_pcm16(self, text: str) -> bytes:
        # F5 writes to a file; cleaner than re-implementing its glue
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out_path = tmp.name
        try:
            self._model.infer(
                ref_file=self._ref_file,
                ref_text=self._ref_text,
                gen_text=text,
                file_wave=out_path,
                show_info=lambda *a, **k: None,  # silence info chatter
            )
            import wave
            with wave.open(out_path, "rb") as wf:
                rate = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
            return resample_pcm16(raw, src_rate=rate, dst_rate=TARGET_SAMPLE_RATE)
        finally:
            Path(out_path).unlink(missing_ok=True)
