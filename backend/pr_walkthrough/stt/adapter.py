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
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from pr_walkthrough.stt.audio import decode_to_float32

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

log = logging.getLogger(__name__)

# Default model. `base` (74M) is fast but routinely returns empty
# transcripts on real-world recordings; `small` (244M) is the smallest
# size that's reliable for English follow-up questions in this app.
# ~500 MB one-time download on first use; ~0.5-1s per short clip on
# CPU after that. Override with PR_WALKTHROUGH_WHISPER_MODEL.
_DEFAULT_MODEL = "small"
_ENV_MODEL_KEY = "PR_WALKTHROUGH_WHISPER_MODEL"
# Voice-activity-detection filter. Defaults OFF — Whisper's bundled
# Silero VAD is aggressive on quiet mics or short clips and is the most
# common cause of "STT returned nothing" on a real, audible recording.
# Set PR_WALKTHROUGH_WHISPER_VAD=1 to re-enable.
_ENV_VAD_KEY = "PR_WALKTHROUGH_WHISPER_VAD"
# If set, dump the raw audio bytes received by transcribe() into this
# directory (one file per call, named with a uuid + the mime extension).
# Useful for "did the browser actually send audio?" debugging.
_ENV_DUMP_KEY = "PR_WALKTHROUGH_STT_DUMP_DIR"
# Language hint passed to Whisper. Default "en" — auto-detect was
# misfiring on short English clips and transcribing as e.g. Welsh or
# Maori. Set "" (empty) to re-enable auto-detect; set "fr"/"de"/etc.
# to lock to another language.
_ENV_LANGUAGE_KEY = "PR_WALKTHROUGH_WHISPER_LANGUAGE"


def _get_model_name() -> str:
    return os.environ.get(_ENV_MODEL_KEY, _DEFAULT_MODEL)


def _vad_enabled() -> bool:
    return os.environ.get(_ENV_VAD_KEY, "0").strip() not in ("", "0", "false", "no")


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

            log.info("Whisper: loading %s (first run downloads weights)…", self._model_name)
            self._model = WhisperModel(
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )
            log.info("Whisper: model ready")
        return self._model

    def warmup(self) -> None:
        """Force model load now (download + weight materialise).

        AppContext calls this at startup so the first voice request
        doesn't pay the model-load latency. No-op once loaded.
        """
        self._load_model()

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
        """Blocking transcription — runs in a thread via asyncio.to_thread.

        Emits diagnostic logs at INFO level so an empty-result run can
        be debugged without rerunning the test: byte count, decoded
        duration, audio RMS (silence vs. speech proxy), VAD setting,
        info.language + duration_after_vad, segment count, raw text.
        """
        t_total = time.perf_counter()

        # 1. Audio dump (opt-in) — saves the raw browser bytes so the
        #    user can play them back and confirm the mic captured what
        #    they thought it captured.
        dump_dir = os.environ.get(_ENV_DUMP_KEY)
        if dump_dir:
            try:
                d = Path(dump_dir)
                d.mkdir(parents=True, exist_ok=True)
                ext = mime.split("/")[-1].split(";")[0] or "bin"
                path = d / f"stt_{uuid.uuid4().hex[:8]}.{ext}"
                path.write_bytes(audio_bytes)
                log.info("STT: dumped %d bytes to %s", len(audio_bytes), path)
            except Exception:
                log.warning("STT: dump failed", exc_info=True)

        # 2. Decode + measure
        t0 = time.perf_counter()
        audio_array = decode_to_float32(audio_bytes, mime)
        decode_ms = (time.perf_counter() - t0) * 1000

        duration_s = len(audio_array) / 16_000  # we resample to 16 kHz
        rms = float(np.sqrt(np.mean(audio_array ** 2))) if len(audio_array) else 0.0
        peak = float(np.max(np.abs(audio_array))) if len(audio_array) else 0.0

        vad_on = _vad_enabled()
        # Empty env value → None (auto-detect). Any other value passed
        # straight through to faster-whisper (ISO 639-1 code).
        lang_env = os.environ.get(_ENV_LANGUAGE_KEY, "en").strip()
        language = lang_env or None
        log.info(
            "STT in: bytes=%d mime=%s decoded=%.2fs (decode=%.0fms) "
            "rms=%.4f peak=%.3f vad=%s lang=%s model=%s",
            len(audio_bytes), mime, duration_s, decode_ms,
            rms, peak, vad_on, language or "auto", self._model_name,
        )

        # Cheap silence sentinel — RMS below this is essentially zero
        # signal. Warn so the user notices when the mic captured
        # nothing audible vs. when STT silently dropped real audio.
        if duration_s > 0 and rms < 0.001:
            log.warning(
                "STT in: audio appears silent (rms=%.5f). "
                "Mic gain too low, wrong input device, or empty recording?",
                rms,
            )

        # 3. Transcribe + measure
        model = self._load_model()
        t1 = time.perf_counter()
        segments_gen, info = model.transcribe(
            audio_array,
            language=language,
            beam_size=5,
            vad_filter=vad_on,
        )
        segments = list(segments_gen)
        transcribe_ms = (time.perf_counter() - t1) * 1000

        text = " ".join(seg.text.strip() for seg in segments).strip()
        confidence = self._aggregate_confidence(segments)

        # 4. Result log — covers the common failure modes:
        #    - segments=0  → VAD ate everything OR audio was silence
        #    - segments>0, text=""  → very rare; usually whitespace-only
        #    - language='nn' with low prob  → mis-detected language
        log.info(
            "STT out: text=%r conf=%.3f segments=%d "
            "lang=%s/%s%% transcribe=%.0fms total=%.0fms duration_after_vad=%.2fs",
            text, confidence, len(segments),
            getattr(info, "language", "?"),
            int(getattr(info, "language_probability", 0.0) * 100),
            transcribe_ms,
            (time.perf_counter() - t_total) * 1000,
            getattr(info, "duration_after_vad", 0.0),
        )
        if not text:
            log.warning(
                "STT returned empty text. Try: "
                "PR_WALKTHROUGH_WHISPER_VAD=0 (disable VAD, default), "
                "PR_WALKTHROUGH_WHISPER_MODEL=small (better accuracy), "
                "PR_WALKTHROUGH_STT_DUMP_DIR=/tmp/stt (inspect input audio)."
            )

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
