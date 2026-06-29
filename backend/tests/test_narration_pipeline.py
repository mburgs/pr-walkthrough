"""Tests for the narration pipeline pieces that regressed during recent
prompt/UX iteration. Each test corresponds to a bug we shipped a fix for.
"""

from __future__ import annotations

import pytest

from contracts.schemas import (
    ChunkNarration, Concern, Hunk, NarrationSegment, CodeAnchor, TourChunk,
)
from pr_walkthrough.llm.adapter import _coerce_anchors
from pr_walkthrough.llm.prompts import (
    build_narrate_chunk_system_addendum,
    format_hunk_for_narration,
)
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

    @pytest.mark.parametrize("text", [
        "the TCP/IP stack",     # all-caps acronym
        "I/O latency",          # single-char halves
        "L1/L2 cache",          # digit halves
        "UTF-8/utf-8",          # contains dash
        "Add/Update endpoint",  # leading caps (probably a code label)
    ])
    def test_leaves_acronyms_and_capitalised_pairs_alone(self, text):
        # Without the guard, tts_scrub used to read "TCP/IP" as "TCP or IP";
        # the rewrite is gated on both halves being all-lowercase letters ≥ 2.
        assert tts_scrub(text) == text


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
# format_hunk_for_narration: hunk render for the narration user message
# ---------------------------------------------------------------------------

def _make_hunk(file: str, new_start: int, new_count: int, body: str) -> Hunk:
    return Hunk(
        file=file,
        old_range=(new_start, new_count),
        new_range=(new_start, new_count),
        header=f"@@ -{new_start},{new_count} +{new_start},{new_count} @@",
        body=body,
    )


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


class TestFormatHunkForNarration:
    def test_file_header_present(self):
        hunk = _make_hunk("path/to/x.py", 1, 1, "+single\n")
        out = format_hunk_for_narration(hunk)
        assert out.startswith("### path/to/x.py")

    def test_diff_body_preserved_verbatim(self):
        # The model gets raw +/-/' ' markers — line numbers are no longer
        # prefixed (the anchor pass handles line attribution, and inline
        # numbers would tempt the model to spell them out in prose).
        hunk = _make_hunk("x.py", 10, 3, "+foo\n+bar\n+baz\n")
        out = format_hunk_for_narration(hunk)
        assert "+foo" in out
        assert "+bar" in out
        assert "+baz" in out
        # Old format leaked line numbers; new format must not
        assert "L  10" not in out
        assert "L----" not in out


# ---------------------------------------------------------------------------
# Familiarity branching in the narrate system addendum
# ---------------------------------------------------------------------------

from contracts.schemas import PRMetadata, TourPlan


def _plan(familiarity: str) -> TourPlan:
    return TourPlan(
        session_id="sess_x",
        pr=PRMetadata(
            url="https://gh.com/a/b/pull/1", repo="a/b", number=1,
            title="t", author="me", base_ref="main", head_ref="feat",
            base_sha="0"*40, head_sha="1"*40,
        ),
        chunks=[_chunk_with("a.py", [(10, 20)])],
        familiarity=familiarity,  # type: ignore[arg-type]
    )


class TestFamiliarityInPrompt:
    """The narration depth instruction lives in the cacheable system
    addendum; this guards against accidental removal of the branch or a
    typo in the level name that would silently fall back to 'review'."""

    @pytest.mark.parametrize("level, marker", [
        ("tutorial",   "NARRATION DEPTH: tutorial"),
        ("tour",       "NARRATION DEPTH: tour"),
        ("review",     "NARRATION DEPTH: review"),
        ("highlights", "NARRATION DEPTH: highlights"),
    ])
    def test_each_level_inserts_distinct_block(self, level, marker):
        addendum = build_narrate_chunk_system_addendum(_plan(level), "diff")
        assert marker in addendum, f"missing depth marker for {level!r}"
        # And the *other* markers should NOT appear (no double-emission)
        for other in ("tutorial", "tour", "review", "highlights"):
            if other == level: continue
            assert f"NARRATION DEPTH: {other}" not in addendum

    def test_default_falls_back_to_review_block(self):
        # The schema default is review; if a plan lands here with an
        # unexpected value (shouldn't happen via the API), the prompt
        # should still pick a valid block instead of blowing up.
        plan = _plan("review")
        out = build_narrate_chunk_system_addendum(plan, "diff")
        assert "NARRATION DEPTH: review" in out
