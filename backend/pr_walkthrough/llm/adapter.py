"""ClaudeLLMAdapter — concrete implementation of LLMAdapter using Anthropic SDK.

Streaming strategy for narrate_chunk
-------------------------------------
narrate_chunk returns a ChunkNarration (the full structured result) AND
exposes token streaming for the narration field so the backend can emit
NarrationTokenEvent SSEs before the full structured response lands.

The interface is:

    narration, token_stream = await adapter.narrate_chunk_streaming(plan, chunk, related)
    # token_stream is an AsyncIterator[str] that yields narration tokens.
    # narration is a ChunkNarration populated after the stream is consumed.

The plain narrate_chunk method is non-streaming and satisfies the LLMAdapter
Protocol. Use narrate_chunk_streaming when the orchestrator wants to emit
SSE tokens while waiting for the full structured result.

Prompt caching
--------------
- The system prompt is cached via a cache_control breakpoint on its block.
- For narrate_chunk, the stable diff-context addendum (plan summary + full diff)
  is placed in a second system block with its own cache_control, so it is only
  billed on the first call per session for each unique plan.
- For answer_follow_up, the system prompt cache covers the largest stable block.
- Minimum cacheable prefix is 1024 tokens on Sonnet 4.6; the system prompt
  alone is well above that threshold.

Model choices
-------------
- plan_tour: claude-opus-4-7 (planning benefits from the strongest model)
- narrate_chunk: claude-sonnet-4-6 (fast, high-quality narration with streaming)
- answer_follow_up: claude-sonnet-4-6 (conversational, low latency)
"""

from __future__ import annotations

import json
import sys
from typing import Any, AsyncIterator

import anthropic
from pydantic import ValidationError

from contracts.schemas import (
    ChunkNarration,
    CodeAnchor,
    Concern,
    FollowUp,
    FollowUpAnswer,
    Highlight,
    Hunk,
    PRMetadata,
    RelatedCode,
    TourChunk,
    TourPlan,
)

from .prompts import (
    SYSTEM_PROMPT,
    build_follow_up_user_message,
    build_narrate_chunk_system_addendum,
    build_narrate_chunk_user_message,
    build_plan_tour_user_message,
    format_hunk_for_plan,
)

# ---------------------------------------------------------------------------
# JSON schemas for tool-use structured output
# ---------------------------------------------------------------------------

_CODE_ANCHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string"},
        "line_range": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
        },
    },
    "required": ["file", "line_range"],
    "additionalProperties": False,
}

_CONCERN_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "text": {"type": "string"},
        "suggested_question": {"type": "string"},
        "anchor": {
            "anyOf": [_CODE_ANCHOR_SCHEMA, {"type": "null"}],
        },
    },
    "required": ["severity", "text", "suggested_question"],
    "additionalProperties": False,
}

_HIGHLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "anchor": _CODE_ANCHOR_SCHEMA,
        "why": {"type": "string"},
    },
    "required": ["anchor", "why"],
    "additionalProperties": False,
}

_RELATED_CODE_SCHEMA = {
    "type": "object",
    "properties": {
        "anchor": _CODE_ANCHOR_SCHEMA,
        "relationship": {
            "type": "string",
            "enum": ["definition", "callsite", "test", "prior_version", "sibling"],
        },
        "snippet": {"type": "string"},
    },
    "required": ["anchor", "relationship", "snippet"],
    "additionalProperties": False,
}

_HUNK_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string"},
        "old_range": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
        },
        "new_range": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
        },
        "header": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["file", "old_range", "new_range", "header", "body"],
    "additionalProperties": False,
}

# LEAN tour-chunk schema for plan_tour. Instead of asking the LLM to echo the
# full Hunk (file/ranges/header/body) for each chunk — which on a multi-thousand-
# line PR easily blows past max_tokens and gets truncated — we ask for hunk_ids
# referencing the indexed diff in the prompt. The orchestrator looks them up
# and reconstitutes a full TourChunk. files[] is similarly redundant (derivable
# from hunk_ids) so we drop it from the LLM contract too.
_LEAN_TOUR_CHUNK_SCHEMA = {
    "type": "object",
    "properties": {
        "chunk_id": {"type": "string"},
        "hunk_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
            "description": "0-based indices into the FULL DIFF list from the prompt",
        },
        "summary": {"type": "string"},
        "rationale_for_position": {"type": "string"},
        "est_concern_level": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": [
        "chunk_id",
        "hunk_ids",
        "summary",
        "rationale_for_position",
        "est_concern_level",
    ],
    "additionalProperties": False,
}

_PR_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "repo": {"type": "string"},
        "number": {"type": "integer"},
        "title": {"type": "string"},
        "author": {"type": "string"},
        "base_ref": {"type": "string"},
        "head_ref": {"type": "string"},
        "base_sha": {"type": "string"},
        "head_sha": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": [
        "url",
        "repo",
        "number",
        "title",
        "author",
        "base_ref",
        "head_ref",
        "base_sha",
        "head_sha",
    ],
    "additionalProperties": False,
}

# Tool schemas for each LLM call
PLAN_TOUR_TOOL = {
    "name": "emit_tour_plan",
    "description": (
        "Emit the ordered tour plan for this pull request. Called exactly once. "
        "Each chunk references diff hunks by their 0-based index from the prompt — "
        "the orchestrator looks them up and attaches the full Hunk objects."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "chunks": {"type": "array", "items": _LEAN_TOUR_CHUNK_SCHEMA},
        },
        "required": ["chunks"],
        "additionalProperties": False,
    },
}

# Guided-tour segment: a few-sentence chunk of narration plus an optional
# anchor that the UI uses to highlight + scroll the diff while the segment
# is being spoken.
_NARRATION_SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "anchor": {"anyOf": [_CODE_ANCHOR_SCHEMA, {"type": "null"}]},
    },
    "required": ["text"],
    "additionalProperties": False,
}

NARRATE_CHUNK_TOOL = {
    "name": "emit_chunk_narration",
    "description": (
        "Emit the narration for one chunk as a guided walkthrough. The reviewer "
        "hears segments in order; each anchored segment makes the UI highlight "
        "and scroll to those lines while it plays. Segments ARE the highlights — "
        "there is no separate highlights field."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "chunk_id": {"type": "string"},
            "segments": {
                "type": "array",
                "items": _NARRATION_SEGMENT_SCHEMA,
                "minItems": 1,
                "description": (
                    "Ordered narration. Aim for 3-6 segments per chunk; most "
                    "narrations need 3-4. Most segments should be anchored to "
                    "specific lines within the chunk's hunks. Reserve unanchored "
                    "segments for one orienting intro, transitions, or genuinely "
                    "big-picture observations."
                ),
            },
            "related_code": {"type": "array", "items": _RELATED_CODE_SCHEMA},
            "concerns": {"type": "array", "items": _CONCERN_SCHEMA},
            "look_closer_for": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "chunk_id", "segments", "related_code", "concerns", "look_closer_for",
        ],
        "additionalProperties": False,
    },
}

ANSWER_FOLLOW_UP_TOOL = {
    "name": "emit_follow_up_answer",
    "description": (
        "Emit the answer to a reviewer's follow-up question. "
        "Called exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "answer_text": {"type": "string"},
            "new_concerns": {"type": "array", "items": _CONCERN_SCHEMA},
            "references": {"type": "array", "items": _CODE_ANCHOR_SCHEMA},
        },
        "required": ["answer_text", "new_concerns", "references"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ClaudeLLMAdapter:
    """Concrete implementation of LLMAdapter backed by the Anthropic Claude API.

    Parameters
    ----------
    api_key:
        Anthropic API key. Defaults to ANTHROPIC_API_KEY env var.
    plan_model:
        Model used for plan_tour. Defaults to claude-opus-4-7 (strongest
        planning capability; one-shot call so cost is acceptable).
    narrate_model:
        Model used for narrate_chunk and answer_follow_up. Defaults to
        claude-sonnet-4-6 (fast, supports streaming, excellent structured output).
    """

    def __init__(
        self,
        api_key: str | None = None,
        plan_model: str = "claude-opus-4-7",
        narrate_model: str = "claude-sonnet-4-6",
    ) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._plan_model = plan_model
        self._narrate_model = narrate_model

    # ------------------------------------------------------------------
    # plan_tour
    # ------------------------------------------------------------------

    async def plan_tour(self, pr: PRMetadata, diff: list[Hunk]) -> TourPlan:
        """Call Claude to produce an ordered TourPlan for the PR.

        The LLM emits a lean response (chunk_id, hunk_ids, summary,
        rationale, severity); the orchestrator reconstitutes the full
        TourPlan by looking hunks up by index from the input diff.
        """
        user_message = build_plan_tour_user_message(pr, diff)

        response = await self._client.messages.create(
            model=self._plan_model,
            max_tokens=4096,  # lean schema — no diff bodies echoed back
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[PLAN_TOUR_TOOL],
            tool_choice={"type": "tool", "name": "emit_tour_plan"},
            messages=[{"role": "user", "content": user_message}],
        )

        raw = self._extract_tool_input(response, "emit_tour_plan")
        return self._reconstitute_plan(pr, diff, raw)

    def _reconstitute_plan(
        self, pr: PRMetadata, diff: list[Hunk], lean: dict
    ) -> TourPlan:
        """Attach full Hunks + PR metadata to the LLM's lean output."""
        lean_chunks = lean.get("chunks", [])
        if not lean_chunks:
            raise ValueError(
                f"Tour plan came back with no chunks. Raw: {json.dumps(lean)[:500]}"
            )

        full_chunks: list[TourChunk] = []
        for lc in lean_chunks:
            hunk_ids = lc.get("hunk_ids") or []
            try:
                hunks = [diff[i] for i in hunk_ids]
            except (IndexError, TypeError) as e:
                raise ValueError(
                    f"Chunk {lc.get('chunk_id')} referenced out-of-range hunk index in "
                    f"{hunk_ids} (diff has {len(diff)} hunks): {e}"
                ) from e
            files = sorted({h.file for h in hunks})
            try:
                full_chunks.append(TourChunk(
                    chunk_id=lc["chunk_id"],
                    files=files,
                    hunks=hunks,
                    summary=lc["summary"],
                    rationale_for_position=lc["rationale_for_position"],
                    est_concern_level=lc["est_concern_level"],
                ))
            except (KeyError, ValidationError) as e:
                raise ValueError(
                    f"Chunk {lc.get('chunk_id', '?')} failed validation:\n{e}"
                ) from e

        return TourPlan(
            session_id=self._make_session_id(pr),
            pr=pr,
            chunks=full_chunks,
        )

    # ------------------------------------------------------------------
    # narrate_chunk (non-streaming)
    # ------------------------------------------------------------------

    async def narrate_chunk(
        self,
        plan: TourPlan,
        chunk: TourChunk,
        related: list[RelatedCode],
    ) -> ChunkNarration:
        """Narrate one chunk. Non-streaming; satisfies LLMAdapter Protocol."""
        diff_context = "\n\n".join(
            f"{h.file} {h.header}\n{h.body}"
            for c in plan.chunks
            for h in c.hunks
        )
        system_addendum = build_narrate_chunk_system_addendum(plan, diff_context)
        user_message = build_narrate_chunk_user_message(chunk, related)

        response = await self._client.messages.create(
            model=self._narrate_model,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": system_addendum,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            tools=[NARRATE_CHUNK_TOOL],
            tool_choice={"type": "tool", "name": "emit_chunk_narration"},
            messages=[{"role": "user", "content": user_message}],
        )

        raw = self._extract_tool_input(response, "emit_chunk_narration")
        return self._parse_chunk_narration(raw)

    # ------------------------------------------------------------------
    # narrate_chunk_streaming
    # ------------------------------------------------------------------

    async def narrate_chunk_streaming(
        self,
        plan: TourPlan,
        chunk: TourChunk,
        related: list[RelatedCode],
    ) -> tuple[ChunkNarration, AsyncIterator[str]]:
        """Narrate one chunk with token streaming on the narration field.

        Returns (ChunkNarration, AsyncIterator[str]).

        The AsyncIterator streams the tokens of the narration text as they
        arrive from the API. The ChunkNarration is populated once streaming
        completes. Callers should consume the iterator to drive the stream;
        the narration object is valid only after the iterator is exhausted.

        Usage::

            narration, tokens = await adapter.narrate_chunk_streaming(plan, chunk, related)
            async for token in tokens:
                await sse_queue.put(NarrationTokenEvent(chunk_id=chunk.chunk_id, text=token))
            # narration is now fully populated

        Note: because tool-use structured output streams via the tool_input
        delta events (not text_delta), we parse the narration field from the
        accumulated JSON incrementally. Tokens emitted are the raw JSON
        characters of the narration string value — we strip the surrounding
        JSON key and quotes and unescape on-the-fly.
        """
        diff_context = "\n\n".join(
            f"{h.file} {h.header}\n{h.body}"
            for c in plan.chunks
            for h in c.hunks
        )
        system_addendum = build_narrate_chunk_system_addendum(plan, diff_context)
        user_message = build_narrate_chunk_user_message(chunk, related)

        # We use a holder so the inner async generator can populate it.
        result_holder: list[ChunkNarration] = []

        async def _token_stream() -> AsyncIterator[str]:
            """Yields decoded narration tokens from the streaming response."""
            accumulated_json = ""
            in_narration = False
            narration_chars: list[str] = []
            # Buffer to detect "narration": " prefix in the JSON stream
            prefix_buf = ""
            NARRATION_KEY = '"narration": "'
            NARRATION_KEY_ALT = '"narration":"'

            async with self._client.messages.stream(
                model=self._narrate_model,
                max_tokens=4096,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": system_addendum,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                tools=[NARRATE_CHUNK_TOOL],
                tool_choice={"type": "tool", "name": "emit_chunk_narration"},
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "partial_json"):
                            chunk_json = delta.partial_json
                            accumulated_json += chunk_json

                            if not in_narration:
                                prefix_buf += chunk_json
                                # Check for narration key in buffered text
                                for key in (NARRATION_KEY, NARRATION_KEY_ALT):
                                    idx = prefix_buf.find(key)
                                    if idx != -1:
                                        in_narration = True
                                        # Emit everything after the key
                                        remainder = prefix_buf[idx + len(key):]
                                        decoded = _unescape_json_string_fragment(remainder)
                                        if decoded:
                                            narration_chars.append(decoded)
                                            yield decoded
                                        prefix_buf = ""
                                        break
                            else:
                                # We're inside the narration string — emit tokens
                                # Stop if we hit the closing quote (unescaped)
                                decoded, done = _extract_narration_fragment(chunk_json, narration_chars)
                                if decoded:
                                    narration_chars.append(decoded)
                                    yield decoded
                                if done:
                                    in_narration = False

                final_msg = await stream.get_final_message()

            # Parse the full structured response
            raw = self._extract_tool_input(final_msg, "emit_chunk_narration")
            result_holder.append(self._parse_chunk_narration(raw))

        gen = _token_stream()

        async def _consume_and_expose() -> AsyncIterator[str]:
            async for tok in gen:
                yield tok

        # We need to return the ChunkNarration after the stream is consumed.
        # We do this by returning a special wrapper that exposes the result.
        wrapper = _StreamWrapper(gen, result_holder)
        return wrapper.get_result, wrapper  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # answer_follow_up
    # ------------------------------------------------------------------

    async def answer_follow_up(
        self,
        plan: TourPlan,
        history: list[ChunkNarration],
        follow_up: FollowUp,
    ) -> FollowUpAnswer:
        """Answer a reviewer's mid-tour question with full session context."""
        user_message = build_follow_up_user_message(plan, history, follow_up)

        response = await self._client.messages.create(
            model=self._narrate_model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[ANSWER_FOLLOW_UP_TOOL],
            tool_choice={"type": "tool", "name": "emit_follow_up_answer"},
            messages=[{"role": "user", "content": user_message}],
        )

        raw = self._extract_tool_input(response, "emit_follow_up_answer")
        try:
            answer = FollowUpAnswer.model_validate(raw)
        except ValidationError as e:
            raise ValueError(
                f"FollowUpAnswer schema mismatch:\n{e}\n\nRaw: {json.dumps(raw, indent=2)}"
            ) from e
        return answer

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tool_input(response: Any, tool_name: str) -> dict:
        """Extract the input dict from a tool_use block in the response.

        Raises ValueError with a clear message if the expected tool call is
        absent — this surfaces schema mismatches at the point of failure.
        """
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input  # type: ignore[return-value]
        stop_reason = getattr(response, "stop_reason", "unknown")
        raise ValueError(
            f"Expected tool_use block '{tool_name}' not found in response. "
            f"stop_reason={stop_reason!r}. "
            f"Content blocks: {[b.type for b in response.content]}"
        )

    @staticmethod
    def _parse_chunk_narration(raw: dict) -> ChunkNarration:
        """Validate and return a ChunkNarration from a raw tool input dict.

        The LLM now emits `segments` instead of a single `narration` string;
        derive `narration` here (joined for transcript/display) so the rest
        of the pipeline still has the plain-prose field it expects.

        Also coerces a few common LLM-produced shape quirks:
          - line_range emitted as [n] (single line) → [n, n]
          - line_range emitted as a single int → [n, n]
        """
        # Concat segments → narration
        if "narration" not in raw and isinstance(raw.get("segments"), list):
            raw = {
                **raw,
                "narration": " ".join(
                    s.get("text", "").strip()
                    for s in raw["segments"]
                    if isinstance(s, dict)
                ),
            }
        # Derive `highlights` from anchored segments — the segments ARE the
        # highlights, the LLM no longer emits them separately.
        if "highlights" not in raw and isinstance(raw.get("segments"), list):
            raw["highlights"] = [
                {"anchor": s["anchor"], "why": s.get("text", "").strip()}
                for s in raw["segments"]
                if isinstance(s, dict) and isinstance(s.get("anchor"), dict)
            ]
        # Coerce malformed anchors anywhere in the payload
        _coerce_anchors(raw)
        try:
            return ChunkNarration.model_validate(raw)
        except ValidationError as e:
            raise ValueError(
                f"ChunkNarration schema mismatch:\n{e}\n\nRaw: {json.dumps(raw, indent=2)}"
            ) from e

    @staticmethod
    def _make_session_id(pr: PRMetadata) -> str:
        """Generate a stable session ID from PR metadata."""
        slug = pr.repo.replace("/", "_") + f"_pr{pr.number}"
        return f"sess_{slug}"


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------

def _coerce_anchors(node: Any) -> None:
    """Walk a dict/list tree and fix anchor.line_range shape quirks in place.

    The LLM sometimes emits a single-line anchor as `line_range: [42]` or
    even `line_range: 42`. The schema wants `[start, end]`. Patch both up.
    """
    if isinstance(node, dict):
        if "anchor" in node and isinstance(node["anchor"], dict):
            anchor = node["anchor"]
            lr = anchor.get("line_range")
            if isinstance(lr, int):
                anchor["line_range"] = [lr, lr]
            elif isinstance(lr, list) and len(lr) == 1 and isinstance(lr[0], int):
                anchor["line_range"] = [lr[0], lr[0]]
        for v in node.values():
            _coerce_anchors(v)
    elif isinstance(node, list):
        for item in node:
            _coerce_anchors(item)


def _unescape_json_string_fragment(fragment: str) -> str:
    """Lightly unescape a JSON string fragment (not a complete string).

    Converts common JSON escapes to their character equivalents.
    Incomplete escape sequences at the fragment boundary are left as-is.
    """
    result = (
        fragment
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )
    return result


def _extract_narration_fragment(
    chunk_json: str,
    accumulated: list[str],
) -> tuple[str, bool]:
    """Extract printable narration text from a JSON stream fragment.

    Returns (decoded_text, is_done).
    is_done is True when the narration string value ends (closing quote seen).
    """
    result_chars: list[str] = []
    i = 0
    done = False
    while i < len(chunk_json):
        ch = chunk_json[i]
        if ch == "\\":
            # Escape sequence
            if i + 1 < len(chunk_json):
                next_ch = chunk_json[i + 1]
                if next_ch == "n":
                    result_chars.append("\n")
                elif next_ch == "t":
                    result_chars.append("\t")
                elif next_ch == '"':
                    result_chars.append('"')
                elif next_ch == "\\":
                    result_chars.append("\\")
                else:
                    result_chars.append(next_ch)
                i += 2
            else:
                # Incomplete escape at boundary — skip
                i += 1
        elif ch == '"':
            # Closing quote of the JSON string value
            done = True
            break
        else:
            result_chars.append(ch)
            i += 1
    return "".join(result_chars), done


class _StreamWrapper:
    """Wraps the streaming async generator so the caller can both iterate
    tokens and retrieve the final ChunkNarration result after exhaustion.

    Usage::

        wrapper = _StreamWrapper(gen, result_holder)
        async for token in wrapper:
            ...
        narration = wrapper.get_result()
    """

    def __init__(self, gen: AsyncIterator[str], result_holder: list[ChunkNarration]) -> None:
        self._gen = gen
        self._result_holder = result_holder

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen

    async def __anext__(self) -> str:
        return await self._gen.__anext__()  # type: ignore[attr-defined]

    def get_result(self) -> ChunkNarration:
        """Return the ChunkNarration. Must be called after the iterator is exhausted."""
        if not self._result_holder:
            raise RuntimeError(
                "Stream not yet fully consumed — call get_result() after iterating."
            )
        return self._result_holder[0]
