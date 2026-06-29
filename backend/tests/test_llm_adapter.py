"""Tests for ClaudeLLMAdapter (Stream 3).

Mock tests (default, no API key needed)
    - test_plan_tour_prompt_construction
    - test_narrate_chunk_prompt_construction
    - test_answer_follow_up_prompt_construction
    - test_structured_output_validation_plan_tour
    - test_structured_output_validation_narrate_chunk
    - test_structured_output_validation_answer_follow_up
    - test_schema_mismatch_raises_value_error
    - test_streaming_wrapper

Live tests (require ANTHROPIC_API_KEY, marked @pytest.mark.live)
    - test_live_plan_tour
    - test_live_narrate_chunk
    - test_live_answer_follow_up
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure contracts and backend are importable
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contracts.schemas import (
    ChunkNarration,
    CodeAnchor,
    Concern,
    FollowUp,
    FollowUpAnswer,
    Hunk,
    PRMetadata,
    RelatedCode,
    TourChunk,
    TourPlan,
)
from pr_walkthrough.llm.adapter import ClaudeLLMAdapter, _StreamWrapper
from pr_walkthrough.llm.prompts import (
    SYSTEM_PROMPT,
    build_follow_up_user_message,
    build_narrate_chunk_user_message,
    build_plan_tour_user_message,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_DIR = _repo_root / "fixtures" / "pr_small"


def _load_fixture_pr() -> PRMetadata:
    return PRMetadata.model_validate(
        json.loads((FIXTURE_DIR / "metadata.json").read_text())
    )


def _load_fixture_diff() -> list[Hunk]:
    data = json.loads((FIXTURE_DIR / "diff.json").read_text())
    return [Hunk.model_validate(h) for h in data]


def _load_fixture_plan() -> TourPlan:
    return TourPlan.model_validate(
        json.loads((FIXTURE_DIR / "tour_plan.json").read_text())
    )


def _load_fixture_narration(chunk_id: str) -> ChunkNarration:
    return ChunkNarration.model_validate(
        json.loads((FIXTURE_DIR / "chunks" / f"{chunk_id}.narration.json").read_text())
    )


@pytest.fixture
def pr():
    return _load_fixture_pr()


@pytest.fixture
def diff():
    return _load_fixture_diff()


@pytest.fixture
def plan():
    return _load_fixture_plan()


@pytest.fixture
def c1_narration():
    return _load_fixture_narration("c1")


@pytest.fixture
def c1_chunk(plan):
    return next(c for c in plan.chunks if c.chunk_id == "c1")


# ---------------------------------------------------------------------------
# Helper: build a mock response with a tool_use block
# ---------------------------------------------------------------------------

def _make_mock_response(tool_name: str, tool_input: dict) -> MagicMock:
    """Build a fake anthropic Message response with one tool_use block."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.input = tool_input

    response = MagicMock()
    response.content = [tool_block]
    response.stop_reason = "tool_use"
    return response


# ---------------------------------------------------------------------------
# Prompt construction tests
# ---------------------------------------------------------------------------

class TestPromptConstruction:
    def test_plan_tour_user_message_contains_pr_title(self, pr, diff):
        msg = build_plan_tour_user_message(pr, diff)
        assert pr.title in msg
        assert pr.author in msg
        assert pr.url in msg

    def test_plan_tour_user_message_contains_all_files(self, pr, diff):
        msg = build_plan_tour_user_message(pr, diff)
        for hunk in diff:
            assert hunk.file in msg

    def test_plan_tour_user_message_contains_diff_body(self, pr, diff):
        msg = build_plan_tour_user_message(pr, diff)
        # At least one hunk body snippet should appear
        assert "rotate" in msg  # from the fixture diff

    def test_narrate_chunk_user_message_contains_chunk_id(self, c1_chunk):
        msg = build_narrate_chunk_user_message(c1_chunk, related=[])
        assert "c1" in msg

    def test_narrate_chunk_user_message_contains_hunk_header(self, c1_chunk):
        msg = build_narrate_chunk_user_message(c1_chunk, related=[])
        for hunk in c1_chunk.hunks:
            assert hunk.header in msg

    def test_narrate_chunk_user_message_with_related_code(self, c1_chunk):
        related = [
            RelatedCode(
                anchor=CodeAnchor(file="src/auth/session.py", line_range=(10, 20)),
                relationship="sibling",
                snippet="class Session:\n    pass",
            )
        ]
        msg = build_narrate_chunk_user_message(c1_chunk, related=related)
        assert "RELATED CODE" in msg
        assert "sibling" in msg
        assert "class Session" in msg

    def test_system_prompt_is_non_empty(self):
        assert len(SYSTEM_PROMPT) > 500

    def test_system_prompt_mentions_tone(self):
        assert "reviewer" in SYSTEM_PROMPT.lower()
        assert "narrat" in SYSTEM_PROMPT.lower()

    def test_follow_up_message_contains_question(self, plan, c1_narration):
        follow_up = FollowUp(chunk_id="c1", question_text="Is rotate() thread-safe?")
        msg = build_follow_up_user_message(plan, [c1_narration], None, [], [], follow_up)
        assert "rotate()" in msg
        assert "thread-safe" in msg

    def test_follow_up_message_contains_history(self, plan, c1_narration):
        follow_up = FollowUp(chunk_id="c1", question_text="Any concerns?")
        msg = build_follow_up_user_message(plan, [c1_narration], None, [], [], follow_up)
        assert "c1" in msg  # history mention

    def test_follow_up_message_low_confidence_note(self, plan):
        follow_up = FollowUp(
            chunk_id="c1",
            question_text="Some question",
            transcript_confidence=0.6,
        )
        msg = build_follow_up_user_message(plan, [], None, [], [], follow_up)
        assert "confidence" in msg.lower()


# ---------------------------------------------------------------------------
# Structured output validation tests
# ---------------------------------------------------------------------------

class TestStructuredOutputValidation:
    """Test that valid tool inputs parse correctly and invalid ones raise."""

    @pytest.mark.asyncio
    async def test_plan_tour_valid_response(self, pr, diff, plan):
        """Mock the lean tool response and verify TourPlan is reconstituted."""
        adapter = ClaudeLLMAdapter(api_key="test-key")

        # Lean schema: per-chunk hunk_ids referencing the input diff list
        lean_chunks = []
        cursor = 0
        for c in plan.chunks:
            n = len(c.hunks)
            lean_chunks.append({
                "chunk_id": c.chunk_id,
                "hunk_ids": list(range(cursor, cursor + n)),
                "summary": c.summary,
                "rationale_for_position": c.rationale_for_position,
                "est_concern_level": c.est_concern_level,
            })
            cursor += n

        mock_response = _make_mock_response("emit_tour_plan", {"chunks": lean_chunks})

        with patch.object(
            adapter._client.messages,
            "create",
            new=AsyncMock(return_value=mock_response),
        ):
            result = await adapter.plan_tour(pr, diff)

        assert isinstance(result, TourPlan)
        assert len(result.chunks) == 3
        # Hunks were reconstituted from the input diff, not echoed by the LLM
        assert sum(len(c.hunks) for c in result.chunks) == len(diff)
        assert result.chunks[0].chunk_id == "c1"

    @pytest.mark.asyncio
    async def test_narrate_chunk_valid_response(self, plan, c1_chunk):
        """Mock the chained narrate + anchor-pass calls and verify composition.

        narrate_chunk emits {intro, body, …}; a second LLM call assigns
        per-sentence anchors. We mock both: the first by tool name, the
        second by tool name. dispatch_by_tool inspects the `tools=`
        kwarg to return the right canned response.
        """
        adapter = ClaudeLLMAdapter(api_key="test-key")

        narrate_raw = {
            "chunk_id": "c1",
            "intro": "This file gains a couple of new helpers.",
            "body": (
                "We're adding rotate to wipe every session for a user. "
                "And touch bumps rotated_at on a single token."
            ),
            "related_code": [],
            "concerns": [],
            "look_closer_for": [],
        }
        # Pick a real (file, line) inside c1_chunk so _validate_assignment
        # doesn't have to snap. Use the first hunk's first line.
        h = c1_chunk.hunks[0]
        anchor_raw = {
            "assignments": [
                {"sentence_index": 0, "file": h.file,
                 "line_start": h.new_range[0], "line_end": h.new_range[0]},
                {"sentence_index": 1, "file": h.file,
                 "line_start": h.new_range[0], "line_end": h.new_range[0]},
            ],
        }

        narrate_resp = _make_mock_response("emit_chunk_narration", narrate_raw)
        anchor_resp = _make_mock_response("assign_sentence_anchors", anchor_raw)

        async def dispatch(**kwargs):
            tool_name = kwargs.get("tools", [{}])[0].get("name", "")
            if tool_name == "emit_chunk_narration":
                return narrate_resp
            if tool_name == "assign_sentence_anchors":
                return anchor_resp
            raise AssertionError(f"unexpected tool call: {tool_name!r}")

        with patch.object(
            adapter._client.messages, "create",
            new=AsyncMock(side_effect=dispatch),
        ):
            result = await adapter.narrate_chunk(plan, c1_chunk, related=[])

        assert isinstance(result, ChunkNarration)
        assert result.chunk_id == "c1"
        # Intro becomes segment[0] with no anchor
        assert result.segments[0].anchor is None
        assert result.segments[0].text == narrate_raw["intro"]
        # Body sentences got merged (both assigned to same line) → one segment
        assert len(result.segments) == 2
        assert result.segments[1].anchor is not None
        # Full prose includes both intro + body
        assert "rotate" in result.narration
        assert "This file gains" in result.narration

    @pytest.mark.asyncio
    async def test_schema_mismatch_raises_value_error_tour_plan(self, pr, diff):
        """A missing required field in Claude's output must raise ValueError."""
        adapter = ClaudeLLMAdapter(api_key="test-key")

        # Empty chunks → reconstitute should reject
        bad_raw = {"chunks": []}
        mock_response = _make_mock_response("emit_tour_plan", bad_raw)

        with pytest.raises((ValueError, Exception)):
            with patch.object(
                adapter._client.messages,
                "create",
                new=AsyncMock(return_value=mock_response),
            ):
                await adapter.plan_tour(pr, diff)

    @pytest.mark.asyncio
    async def test_schema_mismatch_raises_value_error_chunk_narration(self, plan, c1_chunk):
        """A missing required field in narrate_chunk output must raise ValueError."""
        adapter = ClaudeLLMAdapter(api_key="test-key")

        # Missing 'narration' key
        bad_raw = {"chunk_id": "c1"}
        mock_response = _make_mock_response("emit_chunk_narration", bad_raw)

        with pytest.raises((ValueError, Exception)):
            with patch.object(
                adapter._client.messages,
                "create",
                new=AsyncMock(return_value=mock_response),
            ):
                await adapter.narrate_chunk(plan, c1_chunk, related=[])

    @pytest.mark.asyncio
    async def test_missing_tool_use_block_raises_value_error(self, pr, diff):
        """If Claude returns no tool_use block, ValueError is raised."""
        adapter = ClaudeLLMAdapter(api_key="test-key")

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "I cannot do that."
        response = MagicMock()
        response.content = [text_block]
        response.stop_reason = "end_turn"

        with pytest.raises(ValueError, match="emit_tour_plan"):
            with patch.object(
                adapter._client.messages,
                "create",
                new=AsyncMock(return_value=response),
            ):
                await adapter.plan_tour(pr, diff)


# ---------------------------------------------------------------------------
# Streaming wrapper test
# ---------------------------------------------------------------------------

class TestStreamingWrapper:
    @pytest.mark.asyncio
    async def test_stream_wrapper_yields_tokens_and_exposes_result(self, c1_narration):
        """_StreamWrapper yields all tokens and get_result() returns the narration."""
        tokens = ["Hello, ", "world", "!"]
        result_holder: list[ChunkNarration] = [c1_narration]

        async def _gen():
            for t in tokens:
                yield t

        wrapper = _StreamWrapper(_gen(), result_holder)
        collected = []
        async for tok in wrapper:
            collected.append(tok)

        assert collected == tokens
        assert wrapper.get_result() is c1_narration

    def test_stream_wrapper_get_result_before_consume_raises(self, c1_narration):
        """get_result() before consuming the generator raises RuntimeError."""
        async def _gen():
            yield "token"

        wrapper = _StreamWrapper(_gen(), [])
        with pytest.raises(RuntimeError, match="not yet fully consumed"):
            wrapper.get_result()


# ---------------------------------------------------------------------------
# Prompt caching: system blocks have cache_control
# ---------------------------------------------------------------------------

class TestPromptCaching:
    @pytest.mark.asyncio
    async def test_plan_tour_sends_cache_control_on_system(self, pr, diff, plan):
        """plan_tour must include cache_control on the system block."""
        adapter = ClaudeLLMAdapter(api_key="test-key")

        raw_plan = plan.model_dump(mode="json")
        raw_plan["session_id"] = "PENDING"
        mock_response = _make_mock_response("emit_tour_plan", raw_plan)

        captured_kwargs: list[dict] = []

        async def mock_create(**kwargs):
            captured_kwargs.append(kwargs)
            return mock_response

        with patch.object(adapter._client.messages, "create", side_effect=mock_create):
            await adapter.plan_tour(pr, diff)

        assert captured_kwargs, "create() was not called"
        system = captured_kwargs[0]["system"]
        assert isinstance(system, list), "system must be a list of blocks for cache_control"
        cache_blocks = [b for b in system if b.get("cache_control")]
        assert cache_blocks, "At least one system block must have cache_control"

    @pytest.mark.asyncio
    async def test_narrate_chunk_sends_two_cached_system_blocks(self, plan, c1_chunk):
        """narrate_chunk must send two system blocks, both with cache_control.

        narrate_chunk now triggers a follow-up anchor-pass call too; we
        only assert on the first (narration) request's system blocks —
        the anchor pass sends a single one-shot prompt with no system.
        """
        adapter = ClaudeLLMAdapter(api_key="test-key")

        h = c1_chunk.hunks[0]
        narrate_raw = {
            "chunk_id": "c1",
            "intro": None,
            "body": "We add a thing.",
            "related_code": [],
            "concerns": [],
            "look_closer_for": [],
        }
        anchor_raw = {
            "assignments": [
                {"sentence_index": 0, "file": h.file,
                 "line_start": h.new_range[0], "line_end": h.new_range[0]},
            ],
        }
        narrate_resp = _make_mock_response("emit_chunk_narration", narrate_raw)
        anchor_resp = _make_mock_response("assign_sentence_anchors", anchor_raw)

        captured_kwargs: list[dict] = []

        async def mock_create(**kwargs):
            captured_kwargs.append(kwargs)
            tool_name = kwargs.get("tools", [{}])[0].get("name", "")
            return narrate_resp if tool_name == "emit_chunk_narration" else anchor_resp

        with patch.object(adapter._client.messages, "create", side_effect=mock_create):
            await adapter.narrate_chunk(plan, c1_chunk, related=[])

        system = captured_kwargs[0]["system"]
        assert len(system) == 2
        for block in system:
            assert block.get("cache_control"), f"Block missing cache_control: {block}"


# ---------------------------------------------------------------------------
# Live tests (skipped without ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

@pytest.mark.live
class TestLiveClaude:
    """Real Claude API calls. Skipped unless ANTHROPIC_API_KEY is set."""

    @pytest.fixture(autouse=True)
    def require_api_key(self):
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("ANTHROPIC_API_KEY not set")

    @pytest.mark.asyncio
    async def test_live_plan_tour(self):
        pr = _load_fixture_pr()
        diff = _load_fixture_diff()
        adapter = ClaudeLLMAdapter()
        plan = await adapter.plan_tour(pr, diff)
        assert isinstance(plan, TourPlan)
        assert len(plan.chunks) >= 1
        for chunk in plan.chunks:
            assert chunk.chunk_id
            assert chunk.summary
            assert chunk.est_concern_level in ("low", "medium", "high")

    @pytest.mark.asyncio
    async def test_live_narrate_chunk(self):
        plan = _load_fixture_plan()
        chunk = plan.chunks[0]
        adapter = ClaudeLLMAdapter()
        narration = await adapter.narrate_chunk(plan, chunk, related=[])
        assert isinstance(narration, ChunkNarration)
        assert narration.chunk_id == chunk.chunk_id
        assert len(narration.narration) > 50

