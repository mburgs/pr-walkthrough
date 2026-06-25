"""XTTSAdapter — Coqui XTTS-v2 implementation of TTSAdapter.

XTTS-v2 is a multi-speaker neural TTS that takes a reference voice clip
plus text and produces speech in the voice of the reference. We bundle the
F5-TTS reference voice as a sensible default speaker (it's a clear, neutral
English voice that ships with f5-tts).

Heavyweight — ~2GB of model weights, ~5-15s per segment on CPU. Loaded
lazily on first use.

Compatibility note: `coqui-tts` on PyPI imports `isin_mps_friendly` from
transformers, which was removed in transformers v5. Module-level polyfill
restores it before TTS imports happen.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import torch as _torch
import transformers.pytorch_utils as _pu

# Polyfill MUST run before any TTS import in this module
if not hasattr(_pu, "isin_mps_friendly"):
    def _isin_mps_friendly(elements, test_elements):  # type: ignore[no-untyped-def]
        return _torch.isin(elements, test_elements)
    _pu.isin_mps_friendly = _isin_mps_friendly  # type: ignore[attr-defined]

from ._wav import (
    TARGET_SAMPLE_RATE, build_wav_bytes, float32_to_pcm16, resample_pcm16,
)

logger = logging.getLogger(__name__)

# XTTS speaks at 24 kHz natively
_XTTS_RATE = 24000


class XTTSAdapter:
    """Coqui XTTS-v2 adapter.

    The default speaker reference is the F5-TTS bundled English voice; it
    works for both engines and keeps the comparison fair. PR_WALKTHROUGH_XTTS_REF
    can point at a different .wav to use a different speaker.
    """

    def __init__(self) -> None:
        if not self.is_available():
            raise RuntimeError("XTTSAdapter: coqui-tts not installed")
        import os
        from TTS.api import TTS  # type: ignore[import-untyped]

        logger.info("Loading XTTS-v2 (this downloads ~2GB on first run)…")
        # progress_bar=False keeps the log clean
        self._model = TTS(
            model_name="tts_models/multilingual/multi-dataset/xtts_v2",
            progress_bar=False,
        )
        self._ref = os.environ.get(
            "PR_WALKTHROUGH_XTTS_REF",
            self._bundled_reference(),
        )
        self._language = "en"

    @staticmethod
    def _bundled_reference() -> str:
        """Path to the F5-TTS bundled English reference voice."""
        import importlib.util as _u
        spec = _u.find_spec("f5_tts")
        if spec and spec.submodule_search_locations:
            return str(
                __import__("pathlib").Path(spec.submodule_search_locations[0])
                / "infer" / "examples" / "basic" / "basic_ref_en.wav"
            )
        raise RuntimeError("No reference audio found; install f5-tts for the default reference.")

    @classmethod
    def is_available(cls) -> bool:
        import importlib.util as _u
        return _u.find_spec("TTS") is not None

    def available_voices(self) -> list[str]:
        return ["default"]  # Speaker is controlled by the reference file

    def synth(self, text: str, voice: str = "default") -> AsyncIterator[bytes]:
        return self._synth_iter(text)

    async def _synth_iter(self, text: str) -> AsyncIterator[bytes]:
        pcm = await asyncio.to_thread(self._tts_to_pcm16, text)
        yield build_wav_bytes(pcm)

    def _tts_to_pcm16(self, text: str) -> bytes:
        """Run XTTS, return PCM16 at TARGET_SAMPLE_RATE."""
        wav = self._model.tts(
            text=text,
            speaker_wav=self._ref,
            language=self._language,
        )
        pcm24 = float32_to_pcm16(wav)
        return resample_pcm16(pcm24, src_rate=_XTTS_RATE, dst_rate=TARGET_SAMPLE_RATE)
