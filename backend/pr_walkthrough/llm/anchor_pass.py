"""Second-pass anchor assignment.

The first narration pass (narrate_chunk) emits prose + best-effort
anchors per segment. The model is uneven at picking anchors —
sometimes it omits them on segments that should clearly highlight a
specific line; sometimes it picks a range that doesn't quite match
what the prose is about. The bandaid alternatives (snap to nearest
hunk, fall back to first line) are imprecise.

This module replaces those bandaids with a dedicated LLM pass whose
ONLY job is anchor assignment. It receives:
  • the line-numbered diff for the chunk
  • the narration, pre-split into sentences

and returns one (file, line_start, line_end) per sentence — no
exceptions, no nulls. Consecutive sentences mapped to the same
range are then merged into one segment (one highlight, multi-sentence
text), which matches how the player drives diff highlighting.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import anthropic

from contracts.schemas import CodeAnchor, ChunkNarration, NarrationSegment, TourChunk

log = logging.getLogger(__name__)


ASSIGN_ANCHORS_TOOL: dict[str, Any] = {
    "name": "assign_sentence_anchors",
    "description": (
        "Assign each narration sentence to the diff lines it refers to. "
        "Every sentence must be assigned to a (file, line_range) pair from "
        "the chunk's hunks — there is no 'general' option. When several "
        "consecutive sentences talk about the same lines, give them the "
        "SAME range; the orchestrator merges matching consecutive entries "
        "into one highlighted segment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sentence_index": {"type": "integer"},
                        "file": {"type": "string"},
                        "line_start": {"type": "integer"},
                        "line_end": {"type": "integer"},
                    },
                    "required": [
                        "sentence_index", "file", "line_start", "line_end",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["assignments"],
        "additionalProperties": False,
    },
}


# Sentence splitter. Pragmatic: split on `.!?` followed by whitespace +
# capital letter. Misses some abbreviations (e.g. "e.g.") but those are
# rare in narration prose, and one merged sentence is harmless — anchor
# assignment still picks the right line.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"\(\[])")


def split_into_sentences(text: str) -> list[str]:
    """Split prose into sentences for per-sentence anchor assignment."""
    cleaned = text.strip()
    if not cleaned:
        return []
    parts = _SENTENCE_BOUNDARY.split(cleaned)
    return [p.strip() for p in parts if p.strip()]


@dataclass
class AnchorPassMetrics:
    """Diagnostic info from one pass, useful for the eval script."""

    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    sentence_count: int
    segment_count: int


def _line_numbered_hunk_text(chunk: TourChunk) -> str:
    """Render the chunk's hunks with new-side line numbers prefixed.

    The model can only assign anchors to lines that exist on the new
    side of the diff; render the new-side view explicitly so it has no
    excuse to invent line numbers.
    """
    blocks: list[str] = []
    for h in chunk.hunks:
        blocks.append(f"--- {h.file} {h.header}")
        line_no = h.new_range[0]
        for body_line in h.body.splitlines():
            if body_line.startswith("-"):
                # Deletion — exists on old side only, skip line numbering
                continue
            marker = "+" if body_line.startswith("+") else " "
            content = body_line[1:] if body_line else ""
            blocks.append(f"{line_no:>5} {marker} {content}")
            line_no += 1
    return "\n".join(blocks)


def _valid_ranges_summary(chunk: TourChunk) -> str:
    parts: list[str] = []
    for h in chunk.hunks:
        start = h.new_range[0]
        end = h.new_range[0] + max(h.new_range[1] - 1, 0)
        parts.append(f"  • {h.file}: lines {start}-{end}")
    return "\n".join(parts)


def build_anchor_pass_prompt(chunk: TourChunk, sentences: list[str]) -> str:
    """Build the user message for the anchor-assignment pass."""
    numbered_sentences = "\n".join(
        f"[{i}] {s}" for i, s in enumerate(sentences)
    )
    return (
        "You're assigning line-anchors to narration for a guided code "
        "walkthrough. The narration below was already written; your only "
        "job is to attach each sentence to the diff lines it's about.\n\n"
        "RULES\n"
        "-----\n"
        "1. Call `assign_sentence_anchors` with exactly one entry per "
        "sentence (the count must match).\n"
        "2. Every entry must reference a file and line range from the "
        "VALID RANGES list. Don't invent line numbers.\n"
        "3. Line ranges should be tight — pick the lines the sentence is "
        "actually about, not the whole hunk.\n"
        "4. If a sentence is orienting / transitional, pick the lines it's "
        "leading into (or the closest related lines). There's no null/"
        "general option — every sentence gets a range.\n"
        "5. When consecutive sentences talk about the same lines, give "
        "them the SAME (file, line_start, line_end). The orchestrator "
        "merges matching consecutive entries into one highlighted segment, "
        "so this is how you indicate \"these two sentences are one "
        "highlight.\"\n\n"
        f"VALID RANGES\n------------\n{_valid_ranges_summary(chunk)}\n\n"
        f"DIFF (new-side line numbers)\n----------------------------\n"
        f"{_line_numbered_hunk_text(chunk)}\n\n"
        f"NARRATION SENTENCES\n-------------------\n{numbered_sentences}"
    )


def _validate_assignment(
    assignment: dict[str, Any], chunk: TourChunk
) -> CodeAnchor | None:
    """Coerce one assignment into a CodeAnchor, clamping into a real hunk.

    The model may occasionally pick a file/line that doesn't fall inside
    a real hunk (off-by-one, picking a file that isn't in this chunk).
    We clamp to the nearest hunk in the picked file, or fall back to the
    first hunk overall. Returns None if the chunk has zero hunks (which
    shouldn't happen in practice).
    """
    if not chunk.hunks:
        return None

    file = assignment.get("file") or chunk.hunks[0].file
    a_start = int(assignment.get("line_start", 0))
    a_end = int(assignment.get("line_end", a_start))
    if a_end < a_start:
        a_start, a_end = a_end, a_start

    # Find hunks in the picked file
    file_hunks = [h for h in chunk.hunks if h.file == file]
    if not file_hunks:
        # Picked a file that isn't in this chunk — fall back to first hunk
        h = chunk.hunks[0]
        return CodeAnchor(
            file=h.file,
            line_range=(h.new_range[0], h.new_range[0]),
        )

    # Already inside a real hunk?
    for h in file_hunks:
        rs = h.new_range[0]
        re_ = h.new_range[0] + max(h.new_range[1] - 1, 0)
        if rs <= a_start <= re_ or rs <= a_end <= re_ or (a_start <= rs and a_end >= re_):
            # Overlaps this hunk — clamp into it
            new_start = max(rs, a_start)
            new_end = min(re_, a_end)
            if new_end < new_start:
                new_end = new_start
            return CodeAnchor(file=file, line_range=(new_start, new_end))

    # Outside any hunk in this file — snap to the nearest by edge distance
    def gap(h):
        rs = h.new_range[0]
        re_ = h.new_range[0] + max(h.new_range[1] - 1, 0)
        return max(0, rs - a_end, a_start - re_)

    nearest = min(file_hunks, key=gap)
    rs = nearest.new_range[0]
    re_ = nearest.new_range[0] + max(nearest.new_range[1] - 1, 0)
    new_start = max(rs, min(re_, a_start))
    new_end = min(re_, max(new_start, a_end))
    return CodeAnchor(file=file, line_range=(new_start, new_end))


def merge_consecutive_same_anchor(
    sentences: list[str], anchors: list[CodeAnchor | None]
) -> list[NarrationSegment]:
    """Walk sentences in order; group runs sharing the same anchor.

    Returns segments where each segment's text is the joined sentences
    of the run and its anchor is the shared anchor. A None anchor (from
    a chunk with no hunks) yields an unanchored segment, but that case
    shouldn't happen in practice.
    """
    segments: list[NarrationSegment] = []
    if not sentences:
        return segments

    cur_text: list[str] = [sentences[0]]
    cur_anchor = anchors[0]
    for sent, anchor in zip(sentences[1:], anchors[1:]):
        same = (
            cur_anchor is not None
            and anchor is not None
            and cur_anchor.file == anchor.file
            and cur_anchor.line_range == anchor.line_range
        )
        if same:
            cur_text.append(sent)
        else:
            segments.append(
                NarrationSegment(text=" ".join(cur_text), anchor=cur_anchor)
            )
            cur_text = [sent]
            cur_anchor = anchor
    segments.append(
        NarrationSegment(text=" ".join(cur_text), anchor=cur_anchor)
    )
    return segments


async def anchor_sentences(
    sentences: list[str],
    chunk: TourChunk,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
) -> tuple[list[CodeAnchor | None], AnchorPassMetrics]:
    """Run the anchor-assignment LLM pass on pre-split sentences.

    Returns one CodeAnchor per sentence (in input order) plus per-pass
    metrics. Does NOT merge consecutive same-anchor runs — that's the
    caller's job, because the caller has TTS offsets to carry forward
    alongside the merge.
    """
    import time

    if not sentences or not chunk.hunks:
        return [], AnchorPassMetrics(
            model=model, input_tokens=0, output_tokens=0,
            latency_ms=0, sentence_count=0, segment_count=0,
        )

    prompt = build_anchor_pass_prompt(chunk, sentences)
    t0 = time.monotonic()
    response = await client.messages.create(
        model=model,
        max_tokens=2048,
        tools=[ASSIGN_ANCHORS_TOOL],
        tool_choice={"type": "tool", "name": "assign_sentence_anchors"},
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    raw: dict[str, Any] | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "assign_sentence_anchors":
            raw = block.input  # type: ignore[assignment]
            break
    if raw is None:
        raise ValueError(
            f"anchor pass: model {model!r} did not call the tool. "
            f"Response content: {response.content!r}"
        )

    assignments = raw.get("assignments", [])
    by_idx: dict[int, dict[str, Any]] = {}
    for a in assignments:
        idx = int(a.get("sentence_index", -1))
        if 0 <= idx < len(sentences):
            by_idx[idx] = a

    # Fallback for sentences the model skipped: first hunk's first line.
    # The user contract is "every body sentence has an anchor"; this is
    # the defensive fallback that fires only on partial tool output.
    fallback_anchor = CodeAnchor(
        file=chunk.hunks[0].file,
        line_range=(chunk.hunks[0].new_range[0], chunk.hunks[0].new_range[0]),
    )
    anchors: list[CodeAnchor | None] = []
    for i in range(len(sentences)):
        a = by_idx.get(i)
        anchors.append(
            _validate_assignment(a, chunk) if a is not None else fallback_anchor
        )

    metrics = AnchorPassMetrics(
        model=model,
        input_tokens=getattr(response.usage, "input_tokens", 0),
        output_tokens=getattr(response.usage, "output_tokens", 0),
        latency_ms=latency_ms,
        sentence_count=len(sentences),
        segment_count=0,  # not merged here; caller knows
    )
    return anchors, metrics


def merge_with_offsets(
    sentences: list[str],
    anchors: list[CodeAnchor | None],
    sentence_offsets_ms: list[int],
) -> tuple[list[NarrationSegment], list[int]]:
    """Group consecutive same-anchor sentences AND their TTS offsets.

    Returns (segments, segment_offsets_ms) where each segment's offset
    is the first-sentence offset of its group — that's the moment
    audio starts speaking the segment, which is what the player uses
    to drive the diff highlight.

    Lengths: len(sentences) == len(anchors) == len(sentence_offsets_ms)
    coming in; len(segments) == len(segment_offsets_ms) going out.
    """
    if not sentences:
        return [], []
    if len(anchors) != len(sentences) or len(sentence_offsets_ms) != len(sentences):
        raise ValueError(
            "merge_with_offsets: sentence/anchor/offset lengths disagree "
            f"({len(sentences)}/{len(anchors)}/{len(sentence_offsets_ms)})"
        )

    segments: list[NarrationSegment] = []
    seg_offsets: list[int] = []
    cur_text: list[str] = [sentences[0]]
    cur_anchor = anchors[0]
    cur_offset = sentence_offsets_ms[0]
    for sent, anchor, off in zip(
        sentences[1:], anchors[1:], sentence_offsets_ms[1:],
    ):
        same = (
            cur_anchor is not None
            and anchor is not None
            and cur_anchor.file == anchor.file
            and cur_anchor.line_range == anchor.line_range
        )
        if same:
            cur_text.append(sent)
        else:
            segments.append(
                NarrationSegment(text=" ".join(cur_text), anchor=cur_anchor)
            )
            seg_offsets.append(cur_offset)
            cur_text = [sent]
            cur_anchor = anchor
            cur_offset = off
    segments.append(NarrationSegment(text=" ".join(cur_text), anchor=cur_anchor))
    seg_offsets.append(cur_offset)
    return segments, seg_offsets


async def anchor_body_text(
    body_text: str,
    chunk: TourChunk,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
) -> tuple[list[NarrationSegment], AnchorPassMetrics]:
    """Split body text into sentences, anchor each one, merge runs.

    Kept for the non-parallel narrate_chunk path (used by the eval
    script and the plain `narrate_chunk` adapter call). The production
    chunk worker uses `anchor_sentences` + `merge_with_offsets` so it
    can carry per-sentence TTS offsets through the merge.
    """
    sentences = split_into_sentences(body_text)
    if not sentences:
        return [], AnchorPassMetrics(
            model=model, input_tokens=0, output_tokens=0,
            latency_ms=0, sentence_count=0, segment_count=0,
        )

    anchors, metrics = await anchor_sentences(sentences, chunk, client, model)
    segments = merge_consecutive_same_anchor(sentences, anchors)
    metrics = AnchorPassMetrics(
        model=metrics.model,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        latency_ms=metrics.latency_ms,
        sentence_count=metrics.sentence_count,
        segment_count=len(segments),
    )
    return segments, metrics


async def reassign_anchors(
    narration: ChunkNarration,
    chunk: TourChunk,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-sonnet-4-6",
) -> tuple[ChunkNarration, AnchorPassMetrics]:
    """Re-anchor a finished narration. Thin wrapper kept for the eval script.

    Uses every segment's text as the body source, then composes a new
    ChunkNarration with the reassigned segments. Used by
    scripts/eval_anchor_pass.py — production calls `anchor_body_text`
    directly with the raw `body` field from the narration tool.
    """
    full_text = (
        " ".join(s.text for s in narration.segments)
        if narration.segments
        else narration.narration
    )
    segments, metrics = await anchor_body_text(full_text, chunk, client, model)
    return narration.model_copy(update={
        "narration": " ".join(s.text for s in segments),
        "segments": segments,
        "segment_offsets_ms": [],
    }), metrics
