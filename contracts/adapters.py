"""Adapter Protocols — the contract between the orchestrator and each
pluggable subsystem.

Every external concern (LLM, TTS, STT, PR source, context retrieval) sits
behind a Protocol here. Streams 3-7 implement these; stream 2 (orchestrator)
depends only on the Protocols, never on concrete classes.

Async throughout. Streams a) want concurrency under FastAPI and b) some of
these (TTS, LLM) genuinely produce streams of bytes/tokens.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Protocol, runtime_checkable

from contracts.schemas import (
    ChunkNarration,
    CodeAnchor,
    Hunk,
    PRMetadata,
    RelatedCode,
    TourChunk,
    TourPlan,
)


@runtime_checkable
class LLMAdapter(Protocol):
    """Wraps a single LLM provider (Claude).

    The Protocol intentionally only declares the two synchronous-return
    methods. Follow-up Q&A is streaming + tool-using (see
    `ClaudeLLMAdapter.answer_follow_up_streaming` / `FakeLLM`), with a
    larger signature that's awkward to fit into a Protocol; routes that
    need it call the method directly on the concrete adapter via the
    AppContext.
    """

    async def plan_tour(
        self, pr: PRMetadata, diff: list[Hunk]
    ) -> TourPlan: ...

    async def narrate_chunk(
        self,
        plan: TourPlan,
        chunk: TourChunk,
        related: list[RelatedCode],
    ) -> ChunkNarration: ...


@runtime_checkable
class TTSAdapter(Protocol):
    """Local text-to-speech. MUST NOT make network calls."""

    async def synth(
        self, text: str, voice: str = "default"
    ) -> AsyncIterator[bytes]:
        """Yield WAV chunks (22.05kHz, 16-bit mono, single channel).

        The first chunk MUST be a valid WAV header so the browser can
        start playing before the stream completes.
        """
        ...

    def available_voices(self) -> list[str]:
        """Voice names the adapter accepts. Always includes 'default'."""
        ...


@runtime_checkable
class STTAdapter(Protocol):
    """Local speech-to-text. MUST NOT make network calls."""

    async def transcribe(
        self, audio: bytes, mime: str
    ) -> tuple[str, float]:
        """Return (text, confidence in [0, 1])."""
        ...


@runtime_checkable
class PRSource(Protocol):
    """How the system fetches diffs and writes comments back."""

    async def fetch(self, pr_url: str) -> tuple[PRMetadata, list[Hunk]]: ...

    async def post_comment(
        self,
        pr_url: str,
        body: str,
        anchor: CodeAnchor | None = None,
    ) -> str:
        """Post a comment on the PR. Returns the URL of the new comment.

        If anchor is provided, posts as an inline review comment;
        otherwise, posts as a general PR comment.
        """
        ...


@runtime_checkable
class ContextRetriever(Protocol):
    """Pulls related code (definitions, callsites, tests) for an anchor."""

    async def related(
        self, anchor: CodeAnchor, repo_root: Path
    ) -> list[RelatedCode]: ...
