"""FakeTTS — yields a minimal silent WAV for any text."""

from __future__ import annotations

import struct
from typing import AsyncIterator


def _silent_wav(sample_rate: int = 22050, duration_ms: int = 100) -> bytes:
    """Build a valid WAV file containing silence.

    Format: PCM, 22.05kHz, 16-bit, mono.  Duration is configurable but
    defaults to 100 ms — enough that the browser will accept it as audio.
    """
    num_samples = int(sample_rate * duration_ms / 1000)
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = num_samples * block_align
    header_size = 44

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        header_size - 8 + data_size,  # ChunkSize
        b"WAVE",
        b"fmt ",
        16,  # Subchunk1Size (PCM)
        1,  # AudioFormat (PCM = 1)
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + b"\x00" * data_size


class FakeTTS:
    """Satisfies TTSAdapter protocol. Returns a short silent WAV."""

    async def synth(self, text: str, voice: str = "default") -> AsyncIterator[bytes]:
        # Yield the complete WAV in one shot; the header is the first (and only) chunk
        wav_bytes = _silent_wav()
        yield wav_bytes

    def available_voices(self) -> list[str]:
        return ["default"]
