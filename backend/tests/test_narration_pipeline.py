"""Tests for the narration pipeline pieces that regressed during recent
prompt/UX iteration. Each test corresponds to a bug we shipped a fix for.
"""

from __future__ import annotations

import pytest

from contracts.schemas import (
    ChunkNarration, Concern, Hunk, NarrationSegment, CodeAnchor, TourChunk,
)
from pr_walkthrough.llm.adapter import (
    ClaudeLLMAdapter, _coerce_anchors, _snap_anchors_to_chunk_hunks,
)
from pr_walkthrough.llm.prompts import format_hunk_for_narration
from pr_walkthrough.orchestration.chunk_worker import tts_scrub


# ---------------------------------------------------------------------------
# tts_scrub: TTS-friendly text rewrites without breaking file paths
# ---------------------------------------------------------------------------

class TestTtsScrub:
    @pytest.mark.parametrize("text, expected", [
        ("the free/busy feed", "the free or busy feed"),
        ("input/output streams", "input or output streams"),
        ("the read/write split", "the read or write split"),
    ])
    def test_rewrites_word_pair_slashes_to_or(self, text, expected):
        assert tts_scrub(text) == expected

    @pytest.mark.parametrize("text", [
        "src/auth/session.py",                   # multi-slash path
        "open session.py",                       # dotted file
        "https://github.com/x/y",                # URL
        "config.yml and data.json",              # dotted files
        "/usr/local/bin/foo",                    # absolute path
    ])
    def test_leaves_file_paths_alone(self, text):
        assert tts_scrub(text) == text

    def test_strips_markdown_backticks(self):
        # Backticks would otherwise be read aloud as "backtick"
        assert tts_scrub("call `publish` on the `Path`") == "call publish on the Path"

    def test_combines_scrubs_in_one_call(self):
        # Both rewrites in one pass
        assert tts_scrub("a free/busy `Path`") == "a free or busy Path"


# ---------------------------------------------------------------------------
# _coerce_anchors: LLM occasionally emits a single-element line_range
# ---------------------------------------------------------------------------

class TestCoerceAnchors:
    def test_single_element_list_becomes_pair(self):
        raw = {"anchor": {"file": "a.py", "line_range": [42]}}
        _coerce_anchors(raw)
        assert raw["anchor"]["line_range"] == [42, 42]

    def test_scalar_int_becomes_pair(self):
        raw = {"anchor": {"file": "a.py", "line_range": 7}}
        _coerce_anchors(raw)
        assert raw["anchor"]["line_range"] == [7, 7]

    def test_well_formed_pair_is_left_alone(self):
        raw = {"anchor": {"file": "a.py", "line_range": [10, 15]}}
        _coerce_anchors(raw)
        assert raw["anchor"]["line_range"] == [10, 15]

    def test_recurses_into_nested_lists_and_dicts(self):
        raw = {
            "segments": [
                {"text": "x", "anchor": {"file": "a.py", "line_range": [5]}},
                {"text": "y", "anchor": None},
                {"text": "z", "anchor": {"file": "b.py", "line_range": [10, 12]}},
            ],
            "concerns": [
                {"anchor": {"file": "c.py", "line_range": 99}},
            ],
        }
        _coerce_anchors(raw)
        assert raw["segments"][0]["anchor"]["line_range"] == [5, 5]
        assert raw["segments"][1]["anchor"] is None
        assert raw["segments"][2]["anchor"]["line_range"] == [10, 12]
        assert raw["concerns"][0]["anchor"]["line_range"] == [99, 99]


# ---------------------------------------------------------------------------
# _parse_chunk_narration: segments → narration text + anchor coercion
# ---------------------------------------------------------------------------

class TestParseChunkNarration:
    def test_derives_narration_from_segments(self):
        raw = {
            "chunk_id": "c1",
            "segments": [
                {"text": "First segment.", "anchor": None},
                {"text": "Second segment.", "anchor": {"file": "a.py", "line_range": [1, 3]}},
            ],
            "related_code": [],
            "concerns": [],
            "look_closer_for": [],
        }
        out = ClaudeLLMAdapter._parse_chunk_narration(raw)
        assert isinstance(out, ChunkNarration)
        # narration = " ".join(segment texts)
        assert out.narration == "First segment. Second segment."
        assert out.segments == [
            NarrationSegment(text="First segment.", anchor=None),
            NarrationSegment(text="Second segment.", anchor=CodeAnchor(file="a.py", line_range=(1, 3))),
        ]

    def test_handles_single_element_anchor_in_segment(self):
        # Combines the coercion fix with parsing
        raw = {
            "chunk_id": "c1",
            "segments": [
                {"text": "hi", "anchor": {"file": "a.py", "line_range": [42]}},
            ],
            "related_code": [],
            "concerns": [],
            "look_closer_for": [],
        }
        out = ClaudeLLMAdapter._parse_chunk_narration(raw)
        assert out.segments[0].anchor.line_range == (42, 42)


# ---------------------------------------------------------------------------
# format_hunk_for_narration: line-numbered prompt
# ---------------------------------------------------------------------------

def _make_hunk(file: str, new_start: int, new_count: int, body: str) -> Hunk:
    return Hunk(
        file=file,
        old_range=(new_start, new_count),
        new_range=(new_start, new_count),
        header=f"@@ -{new_start},{new_count} +{new_start},{new_count} @@",
        body=body,
    )


class TestFormatHunkForNarration:
    def test_prefixes_added_lines_with_new_side_line_number(self):
        hunk = _make_hunk("x.py", 10, 3, "+foo\n+bar\n+baz\n")
        out = format_hunk_for_narration(hunk)
        assert "L  10  +foo" in out
        assert "L  11  +bar" in out
        assert "L  12  +baz" in out

    def test_marks_deleted_lines_with_dashes_not_numbers(self):
        hunk = _make_hunk("x.py", 10, 2, "-old1\n+new1\n")
        out = format_hunk_for_narration(hunk)
        # Removed lines are unanchorable (anchors live on the new side)
        assert "L----  -old1" in out
        # Inserted line gets the new-side number; the - line does NOT advance
        assert "L  10  +new1" in out

    def test_context_lines_advance_new_side_counter(self):
        hunk = _make_hunk("x.py", 5, 4, " ctx1\n+ins\n ctx2\n+ins2\n")
        out = format_hunk_for_narration(hunk)
        assert "L   5   ctx1" in out
        assert "L   6  +ins" in out
        assert "L   7   ctx2" in out
        assert "L   8  +ins2" in out

    def test_file_header_still_present(self):
        hunk = _make_hunk("path/to/x.py", 1, 1, "+single\n")
        out = format_hunk_for_narration(hunk)
        assert out.startswith("### path/to/x.py")


# ---------------------------------------------------------------------------
# _snap_anchors_to_chunk_hunks: defense-in-depth against drift
# ---------------------------------------------------------------------------

def _chunk_with(file: str, ranges: list[tuple[int, int]]) -> TourChunk:
    """Build a minimal TourChunk with hunks at the given new-side ranges."""
    hunks = [
        Hunk(file=file, old_range=(s, e - s + 1), new_range=(s, e - s + 1),
             header=f"@@ -{s},{e-s+1} +{s},{e-s+1} @@", body="")
        for (s, e) in ranges
    ]
    return TourChunk(
        chunk_id="c1", files=[file], hunks=hunks,
        summary="x", rationale_for_position="x", est_concern_level="low",
    )


def _seg(text: str, anchor: CodeAnchor | None = None) -> NarrationSegment:
    return NarrationSegment(text=text, anchor=anchor)


class TestSnapAnchorsToChunkHunks:
    def test_overlapping_anchor_left_alone(self):
        chunk = _chunk_with("a.py", [(10, 30)])
        n = ChunkNarration(
            chunk_id="c1",
            narration="x",
            segments=[_seg("hi", CodeAnchor(file="a.py", line_range=(12, 14)))],
            related_code=[], concerns=[], look_closer_for=[],
        )
        out = _snap_anchors_to_chunk_hunks(n, chunk)
        assert out.segments[0].anchor.line_range == (12, 14)

    def test_anchor_above_hunk_snaps_to_hunk_start(self):
        chunk = _chunk_with("a.py", [(20, 30)])
        n = ChunkNarration(
            chunk_id="c1",
            narration="x",
            segments=[_seg("hi", CodeAnchor(file="a.py", line_range=(15, 17)))],
            related_code=[], concerns=[], look_closer_for=[],
        )
        out = _snap_anchors_to_chunk_hunks(n, chunk)
        # Snapped: start clamped into the hunk, span preserved (here truncated to fit)
        snapped = out.segments[0].anchor.line_range
        assert snapped == (20, 22)

    def test_anchor_for_wrong_file_is_dropped(self):
        chunk = _chunk_with("a.py", [(10, 30)])
        n = ChunkNarration(
            chunk_id="c1",
            narration="x",
            segments=[_seg("hi", CodeAnchor(file="other.py", line_range=(12, 14)))],
            related_code=[], concerns=[], look_closer_for=[],
        )
        out = _snap_anchors_to_chunk_hunks(n, chunk)
        assert out.segments[0].anchor is None

    def test_concern_anchors_get_same_treatment(self):
        chunk = _chunk_with("a.py", [(20, 30)])
        n = ChunkNarration(
            chunk_id="c1",
            narration="x",
            segments=[_seg("hi")],
            concerns=[Concern(
                severity="medium", text="t", suggested_question="q",
                anchor=CodeAnchor(file="a.py", line_range=(15, 16)),
            )],
            related_code=[], look_closer_for=[],
        )
        out = _snap_anchors_to_chunk_hunks(n, chunk)
        assert out.concerns[0].anchor.line_range == (20, 21)

    def test_unanchored_segment_stays_unanchored(self):
        chunk = _chunk_with("a.py", [(10, 30)])
        n = ChunkNarration(
            chunk_id="c1",
            narration="x",
            segments=[_seg("intro", None)],
            related_code=[], concerns=[], look_closer_for=[],
        )
        out = _snap_anchors_to_chunk_hunks(n, chunk)
        assert out.segments[0].anchor is None
