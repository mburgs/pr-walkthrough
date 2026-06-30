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
        repos_dir: Path = Path.home() / "code",
        cache: object | None = None,
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
            # Parakeet (MLX) is the only supported STT engine. Hard
            # fail on import / load failure rather than silently
            # subbing in a worse adapter — Whisper was tried first and
            # was unusable on real speech, and a silent FakeSTT
            # ("dummy transcription") makes voice features look broken
            # without any signal in the logs.
            #
            # Tests inject FakeSTT directly via the `stt=` ctor arg,
            # so they never hit this path.
            from pr_walkthrough.stt.parakeet_adapter import ParakeetSTTAdapter
            stt = ParakeetSTTAdapter()
            # Warmup eagerly so the model downloads / loads at startup
            # rather than blocking the user's first mic recording.
            stt.warmup()
        if pr_source is None:
            try:
                from pr_walkthrough.pr.gh_source import GhPRSource
                pr_source = GhPRSource()
            except Exception:
                from pr_walkthrough.fakes import FakePRSource
                pr_source = FakePRSource()
        if context_retriever is None:
            # Fail fast if ripgrep isn't installed — the retriever needs
            # it for non-Python file lookups and per-chunk failures
            # produced 20-line tracebacks that drowned the actual logs.
            # Catch only the specific RipgrepNotFoundError and re-raise
            # it cleanly; any other unrelated import failure still falls
            # back to FakeContext so tests / dev environments without
            # jedi still work.
            from pr_walkthrough.context.retriever import (
                RipgrepNotFoundError, ensure_ripgrep_installed,
            )
            ensure_ripgrep_installed()  # raises RipgrepNotFoundError if missing
            try:
                from pr_walkthrough.context.jedi_retriever import (
                    HybridContextRetriever,
                )
                context_retriever = HybridContextRetriever()
            except RipgrepNotFoundError:
                raise
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
        # Persistent content-addressed cache for narration + TTS. None
        # disables caching; the chunk worker treats `ctx.cache is None`
        # as "skip cache, do the work". Opt-in via the user's global
        # config (see pr_walkthrough.config).
        from pr_walkthrough.cache import PersistentCache
        self.cache: PersistentCache | None = cache  # type: ignore[assignment]
        # Parent directory holding repo checkouts as subdirs (e.g. ~/code).
        # The active repo for a session is resolved per-request via
        # `repo_root_for(plan)` below, since one running backend can walk
        # PRs from any number of repos that share a common parent dir.
        self.repos_dir: Path = Path(repos_dir).expanduser()

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

    def repo_root_for(self, plan) -> Path:
        """Resolve the on-disk repo root for a given session's PR.

        We hold a *parent* directory of repo checkouts (e.g. ~/code) so
        one backend can walk PRs from any of the repos under it. The
        repo name comes from the PR slug (plan.pr.repo = 'owner/name')
        — we take the trailing name and join it under repos_dir. Falls
        back to repos_dir itself if the checkout isn't present, which
        lets the rest of the pipeline run (retrieval tools will return
        not-found rather than crash).
        """
        if plan is None or plan.pr is None or not plan.pr.repo:
            return self.repos_dir
        name = plan.pr.repo.split("/")[-1]
        candidate = self.repos_dir / name
        return candidate if candidate.is_dir() else self.repos_dir
