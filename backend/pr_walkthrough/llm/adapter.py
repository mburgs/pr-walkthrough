"""ClaudeLLMAdapter — concrete implementation of LLMAdapter using Anthropic SDK.

Streaming strategy for narrate_chunk
-------------------------------------
narrate_chunk returns a ChunkNarration (the full structured result) AND
exposes token streaming for the narration field so the backend can emit
NarrationTokenEvent SSEs before the full structured response lands.

The interface is:

    stream = await adapter.narrate_chunk_streaming(plan, chunk, related)
    async for token in stream: ...   # token_stream is an AsyncIterator[str]
    narration = stream.get_result()  # ChunkNarration, valid after exhaustion

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
import logging
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import anthropic
from pydantic import ValidationError

from contracts.schemas import (
    ChunkNarration,
    CodeAnchor,
    Flag,
    FollowUp,
    FollowUpAnswer,
    Hunk,
    PRMetadata,
    RelatedCode,
    TourChunk,
    TourPlan,
)

from .prompts import (
    SYSTEM_PROMPT,
    build_follow_up_system_addendum,
    build_follow_up_user_message,
    build_narrate_chunk_system_addendum,
    build_narrate_chunk_user_message,
    build_plan_tour_user_message,
)
from .tools import GREP_REPO_TOOL, READ_FILE_LINES_TOOL, execute_tool

log = logging.getLogger(__name__)

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
            "description": (
                "0-based indices into the FULL DIFF list from the prompt. "
                "A hunk MAY appear in more than one chunk if it provides "
                "essential context for each. Splitting a single file's hunks "
                "across multiple chunks is encouraged when they serve "
                "different narrative roles."
            ),
        },
        "summary": {"type": "string"},
        "rationale_for_position": {"type": "string"},
        "est_concern_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "group": {
            "type": ["string", "null"],
            "description": (
                "Short label (2-4 words) for chunks that share a narrative "
                "purpose — adjacent chunks with the same group are rendered "
                "under a single divider. Examples: 'API surface', "
                "'Mechanism', 'Wiring', 'Tests', 'Config'. Use null when no "
                "useful grouping applies, or when the PR is small enough that "
                "groups would add noise."
            ),
        },
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
        "and scroll to those lines while it plays."
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
                    group=lc.get("group") or None,
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
        result = self._parse_chunk_narration(raw)
        return _snap_anchors_to_chunk_hunks(result, chunk)

    # ------------------------------------------------------------------
    # narrate_chunk_streaming
    # ------------------------------------------------------------------

    async def narrate_chunk_streaming(
        self,
        plan: TourPlan,
        chunk: TourChunk,
        related: list[RelatedCode],
    ) -> "_StreamWrapper":
        """Narrate one chunk with token streaming on the narration field.

        Returns a `_StreamWrapper` which behaves as an `AsyncIterator[str]`
        of decoded narration tokens AND exposes `.get_result()` for the
        completed `ChunkNarration` once the stream is exhausted.

        Usage::

            stream = await adapter.narrate_chunk_streaming(plan, chunk, related)
            async for token in stream:
                await sse_queue.put(NarrationTokenEvent(chunk_id=chunk.chunk_id, text=token))
            narration = stream.get_result()

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
        return _StreamWrapper(gen, result_holder)

    # ------------------------------------------------------------------
    # answer_follow_up
    # ------------------------------------------------------------------

    async def answer_follow_up_streaming(
        self,
        plan: TourPlan,
        narrated_chunks: list[ChunkNarration],
        qa_history: list[tuple[FollowUp, FollowUpAnswer]],
        current_chunk: TourChunk | None,
        related_for_current: list[RelatedCode],
        flags: list[Flag],
        diff_context: str,
        repo_root: Path,
        follow_up: FollowUp,
    ) -> "_FollowUpStream":
        """Stream the follow-up answer with prior Q&A replay + retrieval tools.

        Returns a `_FollowUpStream` — an AsyncIterator[str] of decoded
        characters from the streaming `answer_text` tool_input JSON,
        with a final `.get_result()` for the structured `FollowUpAnswer`
        once the iterator is exhausted.

        Conversation shape:

        - System: SYSTEM_PROMPT + the cached PR/diff/chunk-map addendum.
          Both blocks are marked cacheable so the second-and-later
          follow-ups in a session reuse them.
        - Messages: every prior (question, answer_text) pair is replayed
          as alternating user/assistant turns, then the current user
          message lands last. Replaying past answers as assistant turns
          (rather than stuffing them into the system block) keeps the
          model's sense of "what I have already told this reviewer"
          intact across turns.
        - Tools: `read_file_lines`, `grep_repo`, and the structured
          `emit_follow_up_answer` output tool. tool_choice is left on
          auto — the model decides whether to retrieve before answering.

        Tool-use loop: stream each turn, executing retrieval tools and
        appending their results until the model fires
        `emit_follow_up_answer`. Capped at 5 iterations so a runaway
        model doesn't burn budget. Only `answer_text` characters from
        the answer-tool stream are yielded to the caller — the retrieval
        tools' JSON arguments are not exposed.
        """
        system_addendum = build_follow_up_system_addendum(plan, diff_context)
        first_user_message = build_follow_up_user_message(
            plan=plan,
            narrated_chunks=narrated_chunks,
            current_chunk=current_chunk,
            related_for_current=related_for_current,
            flags=flags,
            follow_up=follow_up,
        )

        # Replay prior Q&A as alternating user/assistant turns, then the
        # current user message. The current message is the only one with
        # diff/related-code context (the prior ones were sent the same
        # way at the time — we don't re-send their bodies here, just the
        # question text — which is fine because the system block holds
        # the full PR + diff that grounds everything).
        messages: list[dict[str, Any]] = []
        for prior_q, prior_a in qa_history:
            messages.append({"role": "user", "content": prior_q.question_text})
            messages.append({"role": "assistant", "content": prior_a.answer_text})
        messages.append({"role": "user", "content": first_user_message})

        system_blocks = [
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
        ]

        result_holder: list[FollowUpAnswer] = []

        async def _token_stream() -> AsyncIterator[str]:
            # Mutable conversation that the loop extends each turn.
            convo: list[dict[str, Any]] = list(messages)
            MAX_ITERS = 5
            for iteration in range(MAX_ITERS):
                # Per-turn state for streaming answer_text tokens.
                # `current_tool` tracks which content block is being built
                # so we only emit chars from emit_follow_up_answer's
                # `answer_text` field — never from a retrieval tool's args.
                current_tool: str | None = None
                in_answer = False
                prefix_buf = ""
                ANSWER_KEY = '"answer_text": "'
                ANSWER_KEY_ALT = '"answer_text":"'

                async with self._client.messages.stream(
                    model=self._narrate_model,
                    max_tokens=4096,
                    system=system_blocks,
                    tools=[
                        READ_FILE_LINES_TOOL,
                        GREP_REPO_TOOL,
                        ANSWER_FOLLOW_UP_TOOL,
                    ],
                    messages=convo,
                ) as stream:
                    async for event in stream:
                        if event.type == "content_block_start":
                            block = event.content_block
                            if getattr(block, "type", None) == "tool_use":
                                current_tool = getattr(block, "name", None)
                                in_answer = False
                                prefix_buf = ""
                            else:
                                current_tool = None
                            continue
                        if event.type == "content_block_stop":
                            current_tool = None
                            in_answer = False
                            prefix_buf = ""
                            continue
                        if event.type != "content_block_delta":
                            continue
                        if current_tool != "emit_follow_up_answer":
                            continue
                        delta = event.delta
                        if not hasattr(delta, "partial_json"):
                            continue
                        chunk_json = delta.partial_json
                        if not in_answer:
                            prefix_buf += chunk_json
                            for key in (ANSWER_KEY, ANSWER_KEY_ALT):
                                idx = prefix_buf.find(key)
                                if idx == -1:
                                    continue
                                in_answer = True
                                remainder = prefix_buf[idx + len(key):]
                                decoded, done = _extract_narration_fragment(remainder, [])
                                if decoded:
                                    yield decoded
                                if done:
                                    in_answer = False
                                prefix_buf = ""
                                break
                        else:
                            decoded, done = _extract_narration_fragment(chunk_json, [])
                            if decoded:
                                yield decoded
                            if done:
                                in_answer = False

                    final_msg = await stream.get_final_message()

                # Walk the final message's tool_use blocks. Two cases:
                #   (a) emit_follow_up_answer — validate, store, we're done.
                #   (b) retrieval tool — execute, collect tool_results,
                #       feed them back in the next loop iteration.
                tool_uses = [
                    b for b in final_msg.content
                    if getattr(b, "type", None) == "tool_use"
                ]
                answer_block = next(
                    (b for b in tool_uses if b.name == "emit_follow_up_answer"),
                    None,
                )
                if answer_block is not None:
                    raw = answer_block.input
                    try:
                        result_holder.append(FollowUpAnswer.model_validate(raw))
                    except ValidationError as e:
                        raise ValueError(
                            f"FollowUpAnswer schema mismatch:\n{e}\n\n"
                            f"Raw: {json.dumps(raw, indent=2)}"
                        ) from e
                    return

                retrieval_uses = [
                    b for b in tool_uses if b.name != "emit_follow_up_answer"
                ]
                if not retrieval_uses:
                    raise ValueError(
                        "Follow-up loop produced neither a retrieval call "
                        "nor an answer. stop_reason="
                        f"{getattr(final_msg, 'stop_reason', 'unknown')!r}"
                    )

                # Append assistant turn + tool_result user turn, then loop.
                convo.append({"role": "assistant", "content": final_msg.content})
                tool_results: list[dict[str, Any]] = []
                for use in retrieval_uses:
                    try:
                        out = execute_tool(use.name, dict(use.input), repo_root)
                    except Exception as e:  # defensive — execute_tool catches its own
                        log.exception("retrieval tool %s raised", use.name)
                        out = f"ERROR: {e}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": out,
                    })
                convo.append({"role": "user", "content": tool_results})

            raise ValueError(
                f"Follow-up tool loop exceeded {MAX_ITERS} iterations without "
                "an answer."
            )

        return _FollowUpStream(_token_stream(), result_holder)

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

def _snap_anchors_to_chunk_hunks(
    narration: ChunkNarration, chunk: TourChunk
) -> ChunkNarration:
    """Validate each segment anchor and snap drift to the nearest real hunk range.

    The line-numbered prompt makes anchors land correctly most of the time, but
    the model still occasionally emits a range that's slightly off (a stray
    off-by-one or a range that pokes outside the actual hunk). For each segment
    anchor we check:

      1. Does the file appear in *this* chunk's hunks? If not, drop the anchor
         — the model picked a file that doesn't belong to this chunk.
      2. Does the line range overlap any hunk's new-side range? If yes, leave
         it alone.
      3. If not, snap to the nearest hunk (by absolute distance from the
         anchor's start line). This catches the common "anchored 2 lines
         above the hunk" case without changing the intent.

    Concern anchors get the same treatment.
    """
    # Build {file: [(start, end), ...]} for this chunk's hunks (new-side)
    hunk_ranges: dict[str, list[tuple[int, int]]] = {}
    for h in chunk.hunks:
        start = h.new_range[0]
        end = h.new_range[0] + max(h.new_range[1] - 1, 0)
        hunk_ranges.setdefault(h.file, []).append((start, end))

    def snap(anchor: CodeAnchor | None) -> CodeAnchor | None:
        if anchor is None:
            return None
        ranges = hunk_ranges.get(anchor.file)
        if not ranges:
            # File isn't in this chunk — drop the anchor rather than mislead the UI
            return None
        a_start, a_end = anchor.line_range
        # Already overlaps a real hunk? Leave alone.
        if any(rs <= a_end and re >= a_start for rs, re in ranges):
            return anchor
        # Snap to the hunk whose interval is nearest the anchor's interval.
        # Use *gap* distance (0 if they touch, else the line count between
        # the closest edges) rather than start-line distance — otherwise an
        # anchor like (100,105) between hunks (10,20) and (200,210) snaps to
        # the wrong hunk because the start-distance heuristic ignores edge
        # proximity.
        def gap(r: tuple[int, int]) -> int:
            rs, re = r
            return max(0, rs - a_end, a_start - re)
        nearest = min(ranges, key=gap)
        # Preserve the relative span of the anchor but clamp inside the hunk
        span = max(0, a_end - a_start)
        new_start = max(nearest[0], min(nearest[1], a_start))
        new_end = min(nearest[1], new_start + span)
        return CodeAnchor(file=anchor.file, line_range=(new_start, new_end))

    new_segments = [
        s.model_copy(update={"anchor": snap(s.anchor)}) for s in narration.segments
    ]
    new_concerns = [
        c.model_copy(update={"anchor": snap(c.anchor)}) for c in narration.concerns
    ]
    return narration.model_copy(update={
        "segments": new_segments,
        "concerns": new_concerns,
    })


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


class _FollowUpStream:
    """Mirror of `_StreamWrapper` for follow-up answers. Same shape:
    async iterator of decoded text characters from the streaming
    `answer_text` JSON field, plus `.get_result()` for the structured
    `FollowUpAnswer` after the iterator is exhausted.
    """

    def __init__(self, gen: AsyncIterator[str], result_holder: list[FollowUpAnswer]) -> None:
        self._gen = gen
        self._result_holder = result_holder

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen

    async def __anext__(self) -> str:
        return await self._gen.__anext__()  # type: ignore[attr-defined]

    def get_result(self) -> FollowUpAnswer:
        if not self._result_holder:
            raise RuntimeError(
                "Stream not yet fully consumed — call get_result() after iterating."
            )
        return self._result_holder[0]
