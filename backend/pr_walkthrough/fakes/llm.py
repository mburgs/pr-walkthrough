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

    async def answer_follow_up(
        self,
        plan: TourPlan,
        history: list[Any],
        follow_up: FollowUp,
    ) -> FollowUpAnswer:
        raw = json.loads((_FIXTURES / "follow_up_example.json").read_text())
        return FollowUpAnswer.model_validate(raw["answer"])
