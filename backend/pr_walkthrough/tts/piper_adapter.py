"""PiperTTSAdapter — fallback using rhasspy/piper (MIT).

Piper is an ONNX-based neural TTS that is fast and self-contained.

Voice models must be downloaded separately (see backend/tts/README.md).
Default voice: en_US-lessac-medium (downloads automatically on first use
if a network connection is available during initialization — or supply
a pre-downloaded model_path).

Output: varies by voice (typically 16 kHz or 22 kHz), resampled to
22 050 Hz, 16-bit mono.
"""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator

from ._wav import (
    CHUNK_SAMPLES,
    TARGET_SAMPLE_RATE,
    build_wav_bytes,
    resample_pcm16,
)

logger = logging.getLogger(__name__)

_DEFAULT_VOICE_NAME = "en_US-lessac-medium"
_PIPER_VOICES_DIR = Path.home() / ".local" / "share" / "piper-voices"


def _ensure_voice(voice_name: str) -> tuple[Path, Path]:
    """Return (model_path, config_path) for *voice_name*.

    Downloads the voice if not already present.  Raises RuntimeError if
    the download fails (e.g. no network).
    """
    model_path = _PIPER_VOICES_DIR / f"{voice_name}.onnx"
    config_path = _PIPER_VOICES_DIR / f"{voice_name}.onnx.json"

    if model_path.exists() and config_path.exists():
        return model_path, config_path

    logger.info("Piper voice '%s' not found locally — downloading…", voice_name)
    try:
        from piper.download_voices import download_voice  # type: ignore[import-untyped]

        _PIPER_VOICES_DIR.mkdir(parents=True, exist_ok=True)
        download_voice(voice_name, _PIPER_VOICES_DIR)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to download Piper voice '{voice_name}': {exc}\n"
            f"Pre-download with:\n"
            f"  python -m piper.download_voices --download-dir {_PIPER_VOICES_DIR} "
            f"  {voice_name}"
        ) from exc

    return model_path, config_path


class PiperTTSAdapter:
    """TTS adapter using Piper (rhasspy/piper).

    *model_path* — if provided, skips automatic voice download and loads
    the model directly.  Useful in air-gapped environments.

    *voice_name* — Piper voice identifier, e.g. 'en_US-lessac-medium'.
    Ignored if *model_path* is supplied.
    """

    def __init__(
        self,
        voice_name: str = _DEFAULT_VOICE_NAME,
        model_path: Path | None = None,
    ) -> None:
        from piper.voice import PiperVoice  # type: ignore[import-untyped]

        if model_path is not None:
            config_path = model_path.with_suffix(".onnx.json")
            if not config_path.exists():
                config_path = model_path.parent / (model_path.stem + ".json")
        else:
            model_path, config_path = _ensure_voice(voice_name)

        logger.info("Loading Piper voice from %s", model_path)
        self._voice = PiperVoice.load(str(model_path), config_path=str(config_path))
        self._voice_name = voice_name
        self._src_rate: int = self._voice.config.sample_rate

    # ------------------------------------------------------------------
    # Protocol compliance
    # ------------------------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        try:
            import piper.voice  # noqa: F401

            return True
        except ImportError:
            return False

    def available_voices(self) -> list[str]:
        return ["default", _DEFAULT_VOICE_NAME]

    def synth(self, text: str, voice: str = "default") -> AsyncIterator[bytes]:
        return self._synth_iter(text, voice)

    async def _synth_iter(self, text: str, voice: str) -> AsyncIterator[bytes]:
        def _run_piper() -> bytes:
            buf = BytesIO()
            import wave

            with wave.open(buf, "wb") as _:
                pass  # won't use this — collect raw PCM

            # synthesize_stream_raw yields numpy arrays of int16 PCM
            pcm_chunks = []
            for audio_chunk in self._voice.synthesize_stream_raw(text):
                pcm_chunks.append(bytes(audio_chunk))
            return b"".join(pcm_chunks)

        all_pcm = await asyncio.to_thread(_run_piper)

        if not all_pcm:
            return

        # Resample to contract sample rate if needed
        if self._src_rate != TARGET_SAMPLE_RATE:
            all_pcm = resample_pcm16(all_pcm, self._src_rate, TARGET_SAMPLE_RATE)

        # Stream in fixed-size chunks; first chunk carries the WAV header
        chunk_bytes = CHUNK_SAMPLES * 2  # 16-bit = 2 bytes/sample
        first_slice = all_pcm[:chunk_bytes]
        yield build_wav_bytes(first_slice)

        offset = chunk_bytes
        while offset < len(all_pcm):
            yield all_pcm[offset : offset + chunk_bytes]
            offset += chunk_bytes
