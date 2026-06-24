"""WhisperSTTAdapter — implements the STTAdapter protocol using faster-whisper.

Confidence heuristic
--------------------
faster-whisper returns a ``no_speech_prob`` and per-segment ``avg_logprob``
values.  We aggregate as follows:

  segment_conf = exp(avg_logprob)          # maps logprob → (0, 1]
  overall_conf = mean(segment_conf_i)      # average across all segments
  final_conf   = overall_conf * (1 - no_speech_penalty)

where ``no_speech_penalty = max(no_speech_prob_i)`` across segments.

Rationale:
- ``avg_logprob`` from Whisper is the mean token log-probability for the
  segment; exp() converts it to a linear probability-like score.
- Averaging across segments is fairer than picking the worst or best.
- Multiplying by ``(1 - no_speech_prob)`` deflates confidence when Whisper
  itself suspects the audio might be silence/noise.
- Clamp to [0.0, 1.0] at the end to be defensive.

Model selection
---------------
Default: ``base`` (~140 MB, good accuracy, fast on M-series CPU).
Override via env var ``PR_WALKTHROUGH_WHISPER_MODEL`` (e.g. ``tiny``).

Models cache in ``~/.cache/huggingface/hub`` on first run.
"""

from __future__ import annotations

import asyncio
import math
import os
from typing import TYPE_CHECKING

from pr_walkthrough.stt.audio import decode_to_float32

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

_DEFAULT_MODEL = "base"
_ENV_MODEL_KEY = "PR_WALKTHROUGH_WHISPER_MODEL"


def _get_model_name() -> str:
    return os.environ.get(_ENV_MODEL_KEY, _DEFAULT_MODEL)


class WhisperSTTAdapter:
    """Local STT using faster-whisper.  Satisfies the STTAdapter protocol.

    The model is loaded lazily on first transcription call so that import
    time stays fast and the download (if needed) happens exactly once.

    Parameters
    ----------
    model_name:
        Whisper model size string (``"tiny"``, ``"base"``, ``"small"``, …).
        Defaults to the value of ``PR_WALKTHROUGH_WHISPER_MODEL`` env var,
        or ``"base"`` if unset.
    device:
        ``"cpu"`` (default) or ``"cuda"``.  Auto-detects on M-series via
        ``"auto"`` but CPU is safer for the compliance-first use case.
    compute_type:
        ``"int8"`` by default — fastest on CPU without significant quality
        loss for base/small models.
    """

    def __init__(
        self,
        model_name: str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self._model_name = model_name or _get_model_name()
        self._device = device
        self._compute_type = compute_type
        self._model: WhisperModel | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_model(self) -> "WhisperModel":
        """Load (or return cached) WhisperModel."""
        if self._model is None:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            self._model = WhisperModel(
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )
        return self._model

    @staticmethod
    def _aggregate_confidence(segments: list) -> float:
        """Convert faster-whisper segment list → single confidence in [0, 1].

        See module docstring for the heuristic explanation.
        """
        if not segments:
            return 0.0

        seg_confs: list[float] = []
        max_no_speech: float = 0.0

        for seg in segments:
            # avg_logprob is negative; exp maps it to (0, 1]
            seg_conf = math.exp(min(seg.avg_logprob, 0.0))
            seg_confs.append(seg_conf)
            no_speech = getattr(seg, "no_speech_prob", 0.0) or 0.0
            max_no_speech = max(max_no_speech, no_speech)

        mean_conf = sum(seg_confs) / len(seg_confs)
        final_conf = mean_conf * (1.0 - max_no_speech)
        return max(0.0, min(1.0, final_conf))

    def _transcribe_sync(self, audio_bytes: bytes, mime: str) -> tuple[str, float]:
        """Blocking transcription — runs in a thread via asyncio.to_thread."""
        model = self._load_model()
        audio_array = decode_to_float32(audio_bytes, mime)

        segments_gen, _info = model.transcribe(
            audio_array,
            language=None,  # auto-detect
            beam_size=5,
            vad_filter=True,  # skip silence at start/end
        )
        # Materialise the generator so we can iterate twice
        segments = list(segments_gen)

        text = " ".join(seg.text.strip() for seg in segments).strip()
        confidence = self._aggregate_confidence(segments)
        return text, confidence

    # ------------------------------------------------------------------
    # STTAdapter protocol
    # ------------------------------------------------------------------

    async def transcribe(self, audio: bytes, mime: str) -> tuple[str, float]:
        """Transcribe *audio* bytes and return ``(text, confidence)``.

        Runs faster-whisper (synchronous) in a thread so this method is
        safe to ``await`` from any asyncio context.

        Parameters
        ----------
        audio:
            Raw audio bytes.  Accepts WebM/Opus, WAV, M4A.
        mime:
            MIME type string (e.g. ``"audio/webm"``, ``"audio/wav"``).

        Returns
        -------
        tuple[str, float]
            ``(transcribed_text, confidence)`` where confidence ∈ [0, 1].
        """
        return await asyncio.to_thread(self._transcribe_sync, audio, mime)
