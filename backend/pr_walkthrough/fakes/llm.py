"""FakeLLM — returns pr_small fixture narrations for any input."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from contracts.schemas import (
    ChunkNarration,
    FollowUp,
    FollowUpAnswer,
    Hunk,
    PRMetadata,
    RelatedCode,
    TourChunk,
    TourPlan,
)

_FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures" / "pr_small"


class FakeLLM:
    """Satisfies LLMAdapter protocol using fixture data."""

    async def plan_tour(self, pr: PRMetadata, diff: list[Hunk]) -> TourPlan:
        raw = json.loads((_FIXTURES / "tour_plan.json").read_text())
        # Assign a fresh session_id and patch the PR metadata to match request
        raw["session_id"] = f"sess_{uuid.uuid4().hex[:12]}"
        raw["pr"] = pr.model_dump()
        return TourPlan.model_validate(raw)

    async def narrate_chunk(
        self,
        plan: TourPlan,
        chunk: TourChunk,
        related: list[RelatedCode],
    ) -> ChunkNarration:
        cid = chunk.chunk_id
        chunk_file = _FIXTURES / "chunks" / f"{cid}.narration.json"
        if chunk_file.exists():
            raw = json.loads(chunk_file.read_text())
        else:
            # Fall back to c1 for any unknown chunk id
            raw = json.loads((_FIXTURES / "chunks" / "c1.narration.json").read_text())
            raw["chunk_id"] = cid
        return ChunkNarration.model_validate(raw)

    async def answer_follow_up_streaming(
        self,
        plan: TourPlan,
        narrated_chunks: list[Any],
        qa_history: list[Any],
        current_chunk: Any,
        related_for_current: list[Any],
        flags: list[Any],
        diff_context: str,
        repo_root: Any,
        follow_up: FollowUp,
    ) -> "_FakeFollowUpStream":
        # Fake ignores all the new context params — fixture answer is fine
        # for routing/transport tests. Real adapter exercises the full shape.
        raw = json.loads((_FIXTURES / "follow_up_example.json").read_text())
        answer = FollowUpAnswer.model_validate(raw["answer"])
        return _FakeFollowUpStream(answer)


class _FakeFollowUpStream:
    """Mirrors the real `_FollowUpStream` shape: an async iterator that
    emits the answer text in word-sized fragments, plus `.get_result()`.
    Word-sized so the consumer can demonstrate the streaming UX without
    a real LLM. Per-token delay kept tiny so tests aren't slow."""

    def __init__(self, answer: FollowUpAnswer) -> None:
        self._answer = answer

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        text = self._answer.answer_text
        # Yield in small chunks so the consumer sees progressive updates
        chunk_size = 4
        for i in range(0, len(text), chunk_size):
            yield text[i : i + chunk_size]

    def get_result(self) -> FollowUpAnswer:
        return self._answer
