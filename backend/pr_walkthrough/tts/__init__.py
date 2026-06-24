"""pr_walkthrough.tts — local TTS adapters.

Public API
----------
make_tts(engine=None)   → TTSAdapter
    Returns the first adapter whose engine is available.
    Pass engine='kokoro' | 'piper' | 'say' to force a specific one.

Adapter classes
---------------
KokoroTTSAdapter   — preferred (hexgrad/kokoro, Apache-2.0, ~300 MB model)
PiperTTSAdapter    — fallback (rhasspy/piper; requires a separate voice download)
SayTTSAdapter      — last resort (macOS `say` + `afconvert`, zero Python deps)

All three implement the TTSAdapter protocol from contracts/adapters.py:
    synth(text, voice='default') -> AsyncIterator[bytes]
        First chunk is a valid RIFF/WAV header + PCM at 22 050 Hz / 16-bit / mono.
    available_voices() -> list[str]
    is_available() -> bool   (classmethod)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

__all__ = [
    "KokoroTTSAdapter",
    "PiperTTSAdapter",
    "SayTTSAdapter",
    "make_tts",
]


def make_tts(engine: str | None = None) -> object:
    """Return the first available TTS adapter.

    Priority order (unless *engine* is specified):
      1. KokoroTTSAdapter  — highest quality
      2. PiperTTSAdapter   — fast, good quality
      3. SayTTSAdapter     — always works on macOS

    Raises RuntimeError if no adapter can be initialised.
    """
    from .kokoro_adapter import KokoroTTSAdapter
    from .piper_adapter import PiperTTSAdapter
    from .say_adapter import SayTTSAdapter

    candidates: list[tuple[str, type]] = [
        ("kokoro", KokoroTTSAdapter),
        ("piper", PiperTTSAdapter),
        ("say", SayTTSAdapter),
    ]

    if engine is not None:
        candidates = [(n, cls) for n, cls in candidates if n == engine]
        if not candidates:
            raise ValueError(f"Unknown engine '{engine}'. Choose from: kokoro, piper, say")

    errors: list[str] = []
    for name, cls in candidates:
        if not cls.is_available():
            errors.append(f"{name}: not installed")
            continue
        try:
            adapter = cls()
            logger.info("TTS: using %s", name)
            return adapter
        except Exception as exc:
            errors.append(f"{name}: init failed — {exc}")
            logger.warning("TTS adapter '%s' failed to init: %s", name, exc)

    raise RuntimeError(
        "No TTS adapter could be initialised.\n"
        + "\n".join(f"  {e}" for e in errors)
        + "\nInstall at least one engine:\n"
        "  pip install -e .[kokoro]   # Kokoro\n"
        "  pip install -e .[piper]    # Piper\n"
        "  (SayTTSAdapter ships with macOS — no extra install needed)"
    )


# Lazy imports so that missing optional deps don't break `import pr_walkthrough.tts`
def __getattr__(name: str) -> type:
    if name == "KokoroTTSAdapter":
        from .kokoro_adapter import KokoroTTSAdapter

        return KokoroTTSAdapter
    if name == "PiperTTSAdapter":
        from .piper_adapter import PiperTTSAdapter

        return PiperTTSAdapter
    if name == "SayTTSAdapter":
        from .say_adapter import SayTTSAdapter

        return SayTTSAdapter
    raise AttributeError(f"module 'pr_walkthrough.tts' has no attribute {name!r}")
