"""AppContext — dependency-injection container.

Holds one instance of every adapter. Default ctor wires up fakes.
Real adapters are plugged in by overriding the constructor args.
"""

from __future__ import annotations

from pathlib import Path

from contracts.adapters import (
    ContextRetriever,
    LLMAdapter,
    PRSource,
    STTAdapter,
    TTSAdapter,
)
from pr_walkthrough.store import SessionStore


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
        db_path: str | Path = "sessions.db",
        repo_root: Path = Path("."),
    ) -> None:
        # Import fakes lazily so real adapters can be passed without importing fakes
        if llm is None:
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
            from pr_walkthrough.fakes import FakeSTT
            stt = FakeSTT()
        if pr_source is None:
            from pr_walkthrough.fakes import FakePRSource
            pr_source = FakePRSource()
        if context_retriever is None:
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
