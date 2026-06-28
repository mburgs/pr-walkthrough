"""AppContext — dependency-injection container.

Holds one instance of every adapter. Default ctor wires up fakes.
Real adapters are plugged in by overriding the constructor args.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from contracts.adapters import (
    ContextRetriever,
    LLMAdapter,
    PRSource,
    STTAdapter,
    TTSAdapter,
)
from pr_walkthrough.orchestration.throttle import (
    resolve_llm_concurrency,
    resolve_tts_concurrency,
)
from pr_walkthrough.store import SessionStore

log = logging.getLogger(__name__)


class AppContext:
    """Passed to every route via FastAPI dependency injection."""

    def __init__(
        self,
        llm: LLMAdapter | None = None,
        tts: TTSAdapter | None = None,
        stt: STTAdapter | None = None,
        pr_source: PRSource | None = None,
        context_retriever: ContextRetriever | None = None,
        store: SessionStore | None = None,
        tts_registry: object | None = None,
        db_path: str | Path = "sessions.db",
        repo_root: Path = Path("."),
    ) -> None:
        # Import fakes lazily so real adapters can be passed without importing fakes
        if llm is None:
            if os.environ.get("ANTHROPIC_API_KEY"):
                try:
                    from pr_walkthrough.llm.adapter import ClaudeLLMAdapter
                    # Sonnet for planning too — keeps wait under a minute even
                    # on multi-thousand-line PRs. Opus is overkill for grouping +
                    # ordering hunks. PR_WALKTHROUGH_PLAN_MODEL overrides.
                    plan_model = os.environ.get(
                        "PR_WALKTHROUGH_PLAN_MODEL", "claude-sonnet-4-6"
                    )
                    llm = ClaudeLLMAdapter(plan_model=plan_model)
                except Exception:
                    from pr_walkthrough.fakes import FakeLLM
                    llm = FakeLLM()
            else:
                from pr_walkthrough.fakes import FakeLLM
                llm = FakeLLM()
        if tts is None:
            try:
                from pr_walkthrough.tts import make_tts
                tts = make_tts()
            except Exception:
                # No real engine available (e.g. non-macOS CI without
                # kokoro/piper) — fall back to the silent fake so the rest of
                # the app keeps working.
                from pr_walkthrough.fakes import FakeTTS
                tts = FakeTTS()
        if stt is None:
            # STT engine selection. Default whisper; flip to parakeet for
            # better English accuracy on M-series Macs. Each engine's
            # import + load is wrapped so a missing dependency degrades
            # to FakeSTT instead of crashing the whole app.
            engine = os.environ.get("PR_WALKTHROUGH_STT_ENGINE", "whisper").lower()
            if engine == "parakeet":
                try:
                    from pr_walkthrough.stt.parakeet_adapter import ParakeetSTTAdapter
                    stt = ParakeetSTTAdapter()
                except Exception:
                    log.exception("Parakeet STT unavailable — falling back to FakeSTT")
                    from pr_walkthrough.fakes import FakeSTT
                    stt = FakeSTT()
            else:
                try:
                    from pr_walkthrough.stt.adapter import WhisperSTTAdapter
                    stt = WhisperSTTAdapter()
                except Exception:
                    # faster-whisper not installable or model fetch failed — fall
                    # back to the dummy so the rest of the app still works.
                    from pr_walkthrough.fakes import FakeSTT
                    stt = FakeSTT()
            # Eagerly warm the model so the download + load happens at
            # startup, not on the user's first mic recording (would be a
            # 5-30s surprise wait). Adapters implement warmup() to do
            # this; FakeSTT/other shim adapters just won't have it.
            warmup = getattr(stt, "warmup", None)
            if callable(warmup):
                try:
                    warmup()
                except Exception:
                    log.exception("STT warmup failed; first call will pay the load cost")
        if pr_source is None:
            try:
                from pr_walkthrough.pr.gh_source import GhPRSource
                pr_source = GhPRSource()
            except Exception:
                from pr_walkthrough.fakes import FakePRSource
                pr_source = FakePRSource()
        if context_retriever is None:
            try:
                from pr_walkthrough.context.jedi_retriever import (
                    HybridContextRetriever,
                )
                context_retriever = HybridContextRetriever()
            except Exception:
                from pr_walkthrough.fakes import FakeContext
                context_retriever = FakeContext()
        if store is None:
            store = SessionStore(db_path)

        self.llm: LLMAdapter = llm
        self.tts: TTSAdapter = tts
        self.stt: STTAdapter = stt
        self.pr_source: PRSource = pr_source
        self.context: ContextRetriever = context_retriever
        self.store: SessionStore = store
        self.repo_root: Path = Path(repo_root)

        # Multi-engine registry for the audio-variants A/B endpoint. Lazy:
        # engines are instantiated on first request, not at startup. Tests
        # can inject their own registry (with fake engines) via the
        # tts_registry kwarg.
        if tts_registry is not None:
            self.tts_registry = tts_registry
        else:
            try:
                from pr_walkthrough.tts.registry import build_default_registry
                self.tts_registry = build_default_registry()
            except Exception:
                self.tts_registry = None

        # Concurrency caps for the two expensive operations the worker
        # does. Kept here on AppContext (not module-global) so tests can
        # construct an isolated context with custom limits and so the
        # values are inspectable for diagnostics. Resolved at construction
        # time from env / auto-detected RAM — see orchestration/throttle.py.
        self.tts_concurrency: int = resolve_tts_concurrency()
        self.llm_concurrency: int = resolve_llm_concurrency()
        self.tts_semaphore: asyncio.Semaphore = asyncio.Semaphore(self.tts_concurrency)
        self.llm_semaphore: asyncio.Semaphore = asyncio.Semaphore(self.llm_concurrency)
        log.info(
            "concurrency caps: tts=%d llm=%d (override via "
            "PR_WALKTHROUGH_{TTS,LLM}_CONCURRENCY)",
            self.tts_concurrency, self.llm_concurrency,
        )
