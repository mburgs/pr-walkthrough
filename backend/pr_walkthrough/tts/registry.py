"""Registry of TTS engines available to the orchestrator.

The single `make_tts()` factory still picks one default engine for the
eager-narration path; this registry is the multi-engine view used by the
audio-variants API. Each engine is lazy-loaded on first request (most
weigh >= 1 GB).
"""

from __future__ import annotations

import logging
from typing import Protocol

from contracts.adapters import TTSAdapter

logger = logging.getLogger(__name__)


class TTSFactory(Protocol):
    def __call__(self) -> TTSAdapter: ...


class TTSRegistry:
    """Lazy registry. Each engine constructor only fires on first use."""

    def __init__(self) -> None:
        self._instances: dict[str, TTSAdapter] = {}
        self._factories: dict[str, TTSFactory] = {}

    def register(self, name: str, factory: TTSFactory) -> None:
        self._factories[name] = factory

    def known(self) -> list[str]:
        return sorted(self._factories.keys())

    def available(self) -> list[str]:
        """Engines whose dependencies are installed (cheap probe)."""
        out: list[str] = []
        for name, factory in self._factories.items():
            cls = getattr(factory, "__self__", factory)
            is_available = getattr(cls, "is_available", None)
            if is_available is None or is_available():
                out.append(name)
        return sorted(out)

    def get(self, name: str) -> TTSAdapter:
        """Return the engine, instantiating on first call. Raises on unknown."""
        if name not in self._factories:
            raise KeyError(f"Unknown TTS engine: {name!r}; have {self.known()}")
        if name not in self._instances:
            logger.info("TTSRegistry: instantiating %s", name)
            self._instances[name] = self._factories[name]()
        return self._instances[name]


def build_default_registry() -> TTSRegistry:
    """Wire up every engine we know about. Caller picks which to USE."""
    reg = TTSRegistry()

    # Kokoro — class itself acts as factory
    from .kokoro_adapter import KokoroTTSAdapter
    reg.register("kokoro", KokoroTTSAdapter)

    # XTTS-v2 — only if coqui-tts is installed
    try:
        from .xtts_adapter import XTTSAdapter
        if XTTSAdapter.is_available():
            reg.register("xtts", XTTSAdapter)
    except Exception as exc:
        logger.info("XTTS not registered: %s", exc)

    # F5-TTS — only if f5-tts is installed
    try:
        from .f5_adapter import F5TTSAdapter
        if F5TTSAdapter.is_available():
            reg.register("f5", F5TTSAdapter)
    except Exception as exc:
        logger.info("F5-TTS not registered: %s", exc)

    return reg
