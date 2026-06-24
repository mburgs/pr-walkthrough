"""WAV header and chunk utilities.

All TTS output is pinned to 22050 Hz, 16-bit, mono per contracts/api.md.
"""

from __future__ import annotations

import struct
import wave
from io import BytesIO

TARGET_SAMPLE_RATE: int = 22050
SAMPLE_WIDTH: int = 2  # 16-bit
CHANNELS: int = 1
CHUNK_SAMPLES: int = 4096  # samples per streaming chunk (~185 ms at 22050 Hz)


def build_wav_header(num_frames: int) -> bytes:
    """Return a 44-byte RIFF/WAV header for the given frame count."""
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(TARGET_SAMPLE_RATE)
        wf.setnframes(num_frames)
    return buf.getvalue()


def build_wav_bytes(pcm: bytes) -> bytes:
    """Wrap raw 16-bit mono PCM bytes in a complete WAV container."""
    num_frames = len(pcm) // SAMPLE_WIDTH
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(TARGET_SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def float32_to_pcm16(samples) -> bytes:  # type: ignore[type-arg]
    """Convert a float32 numpy/tensor array (range ±1) to 16-bit PCM bytes."""
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767).astype(np.int16)
    return pcm.tobytes()


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """Resample 16-bit mono PCM to the target sample rate using linear interpolation.

    Used to down/upsample kokoro (24 kHz) → contract spec (22 050 Hz).
    For production quality a proper resampler (soxr) is preferred; this is
    good enough for speech.
    """
    if src_rate == dst_rate:
        return pcm

    import numpy as np

    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    src_len = len(arr)
    dst_len = int(src_len * dst_rate / src_rate)
    indices = np.linspace(0, src_len - 1, dst_len)
    lo = np.floor(indices).astype(int)
    hi = np.minimum(lo + 1, src_len - 1)
    frac = (indices - lo).astype(np.float32)
    resampled = arr[lo] * (1 - frac) + arr[hi] * frac
    return resampled.astype(np.int16).tobytes()
