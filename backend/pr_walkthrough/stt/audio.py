"""Audio decoding and normalisation.

Accepts WebM/Opus (browser MediaRecorder default), WAV, and M4A.
Converts to 16 kHz mono float32 numpy array — the format faster-whisper
expects for its transcribe() call.

Why pydub + ffmpeg instead of librosa/soundfile?
- pydub handles WebM/Opus natively through ffmpeg (soundfile cannot).
- librosa pulls in a large dependency tree (resampy/soxr) not needed here.
- pydub is already a common FastAPI audio dependency so it's low-surprise.
- The only system requirement is ffmpeg, which is already listed as required.
"""

from __future__ import annotations

import io
import math

import numpy as np
from pydub import AudioSegment

TARGET_SAMPLE_RATE = 16_000  # Hz — Whisper's native rate


def decode_to_float32(audio_bytes: bytes, mime: str = "audio/webm") -> np.ndarray:
    """Decode *audio_bytes* to a 16 kHz mono float32 numpy array.

    Parameters
    ----------
    audio_bytes:
        Raw audio bytes from the client (WebM/Opus, WAV, or M4A).
    mime:
        MIME type hint used to choose the pydub format string.
        Supported values: ``audio/webm``, ``audio/wav``, ``audio/x-wav``,
        ``audio/m4a``, ``audio/mp4``, ``audio/mpeg``.

    Returns
    -------
    np.ndarray
        Float32 array, shape ``(n_samples,)``, values in ``[-1.0, 1.0]``.
    """
    fmt = _mime_to_pydub_format(mime)
    seg: AudioSegment = AudioSegment.from_file(io.BytesIO(audio_bytes), format=fmt)

    # Normalise to 16 kHz mono
    seg = seg.set_frame_rate(TARGET_SAMPLE_RATE).set_channels(1)

    # pydub raw samples are signed 16-bit PCM integers
    raw = np.frombuffer(seg.raw_data, dtype=np.int16)
    return raw.astype(np.float32) / float(math.pow(2, 15))


def _mime_to_pydub_format(mime: str) -> str:
    """Map a MIME type to a pydub/ffmpeg format string."""
    mime = mime.split(";")[0].strip().lower()
    mapping: dict[str, str] = {
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/wave": "wav",
        "audio/m4a": "m4a",
        "audio/mp4": "mp4",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
    }
    fmt = mapping.get(mime)
    if fmt is None:
        # Fall back: let ffmpeg auto-detect; pydub accepts "" or None
        return "webm"
    return fmt
