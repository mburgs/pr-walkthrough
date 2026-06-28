"""ParakeetSTTAdapter — STT via NVIDIA Parakeet running on Apple Silicon (MLX).

Parakeet is engineered for low-latency English ASR and on M-series Macs
the MLX port is meaningfully faster than Whisper-medium with better
accuracy on natural speech (the motivating use case here: short
follow-up questions where Whisper-base/small/medium kept whiffing).

Trade-offs vs. WhisperSTTAdapter:
  + English-only — matches this app's needs, simpler defaults
  + Fast on M-series via MLX (no GPU needed)
  + More reliable on short clips that Whisper auto-detect garbles
  - Heavier deps: parakeet-mlx + mlx (~bf16 model ~600 MB download)
  - Path-based API only — we round-trip bytes through a temp file

Selection
---------
Picked by AppContext when ``PR_WALKTHROUGH_STT_ENGINE=parakeet``.
Falls back gracefully to FakeSTT if ``parakeet_mlx`` isn't importable
(e.g. running on Linux without MLX).

Env knobs
---------
``PR_WALKTHROUGH_PARAKEET_MODEL``   HF id, default
                                    ``mlx-community/parakeet-tdt-0.6b-v2``
``PR_WALKTHROUGH_STT_DUMP_DIR``     shared with Whisper adapter
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from parakeet_mlx import BaseParakeet

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"
_ENV_MODEL_KEY = "PR_WALKTHROUGH_PARAKEET_MODEL"
_ENV_DUMP_KEY = "PR_WALKTHROUGH_STT_DUMP_DIR"


def _get_model_name() -> str:
    return os.environ.get(_ENV_MODEL_KEY, _DEFAULT_MODEL)


def _mime_extension(mime: str) -> str:
    """Best-effort file extension for a MIME type — parakeet-mlx hands
    the path to ffmpeg, which sniffs the container, so the extension
    just needs to be plausible."""
    base = mime.split(";")[0].strip().lower()
    return base.split("/")[-1] or "bin"


class ParakeetSTTAdapter:
    """Satisfies the STTAdapter protocol with parakeet-mlx.

    Parameters mirror the Whisper adapter for parity: ``model_name``
    is the only knob that materially matters; device/compute_type are
    accepted for interface compatibility but ignored (MLX picks the
    Metal device automatically).
    """

    def __init__(
        self,
        model_name: str | None = None,
        device: str = "mlx",         # accepted for protocol parity, ignored
        compute_type: str = "bf16",  # likewise; the dtype is fixed by model
    ) -> None:
        self._model_name = model_name or _get_model_name()
        self._device = device
        self._compute_type = compute_type
        self._model: BaseParakeet | None = None

    def _load_model(self) -> "BaseParakeet":
        if self._model is None:
            from parakeet_mlx import from_pretrained  # noqa: PLC0415
            log.info("Parakeet: loading %s (first run downloads weights)…", self._model_name)
            self._model = from_pretrained(self._model_name)
            log.info("Parakeet: model ready")
        return self._model

    def warmup(self) -> None:
        """Force model load now (download + weight materialise).

        AppContext calls this at startup so the first voice request
        doesn't pay the multi-second model load latency. No-op once
        the model is cached in memory.
        """
        self._load_model()

    @staticmethod
    def _aggregate_confidence(result) -> float:
        """Mean per-sentence confidence; 0.0 when there are no sentences."""
        sentences = getattr(result, "sentences", None) or []
        if not sentences:
            return 0.0
        confs = [float(getattr(s, "confidence", 0.0) or 0.0) for s in sentences]
        if not confs:
            return 0.0
        return max(0.0, min(1.0, sum(confs) / len(confs)))

    def _transcribe_sync(self, audio_bytes: bytes, mime: str) -> tuple[str, float]:
        """Blocking transcription — runs via asyncio.to_thread.

        Writes the bytes to a temp file because parakeet-mlx's API
        takes a path (it shells out to ffmpeg for decoding, which
        handles webm/opus natively). Logs the same shape of
        diagnostics as the Whisper adapter so empty-result debugging
        is consistent across engines.
        """
        t_total = time.perf_counter()

        dump_dir = os.environ.get(_ENV_DUMP_KEY)
        if dump_dir:
            try:
                d = Path(dump_dir)
                d.mkdir(parents=True, exist_ok=True)
                ext = _mime_extension(mime)
                path = d / f"stt_{uuid.uuid4().hex[:8]}.{ext}"
                path.write_bytes(audio_bytes)
                log.info("STT: dumped %d bytes to %s", len(audio_bytes), path)
            except Exception:
                log.warning("STT: dump failed", exc_info=True)

        # Write to a NamedTemporaryFile with a sensible extension. We
        # could keep these around for inspection (see DUMP_DIR above)
        # but the temp is for parakeet-mlx alone.
        ext = _mime_extension(mime)
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
            tf.write(audio_bytes)
            tmp_path = tf.name

        log.info(
            "STT in: engine=parakeet bytes=%d mime=%s tmp=%s model=%s",
            len(audio_bytes), mime, tmp_path, self._model_name,
        )

        try:
            model = self._load_model()
            t1 = time.perf_counter()
            result = model.transcribe(tmp_path)
            transcribe_ms = (time.perf_counter() - t1) * 1000
        finally:
            try:
                Path(tmp_path).unlink()
            except Exception:
                pass

        text = (getattr(result, "text", "") or "").strip()
        confidence = self._aggregate_confidence(result)
        sentences = getattr(result, "sentences", None) or []

        log.info(
            "STT out: engine=parakeet text=%r conf=%.3f sentences=%d "
            "transcribe=%.0fms total=%.0fms",
            text, confidence, len(sentences),
            transcribe_ms, (time.perf_counter() - t_total) * 1000,
        )
        if not text:
            log.warning(
                "Parakeet returned empty text. Try a longer/clearer recording, "
                "or fall back to Whisper via PR_WALKTHROUGH_STT_ENGINE=whisper."
            )
        return text, confidence

    async def transcribe(self, audio: bytes, mime: str) -> tuple[str, float]:
        """Transcribe *audio* bytes; same shape as WhisperSTTAdapter."""
        return await asyncio.to_thread(self._transcribe_sync, audio, mime)
