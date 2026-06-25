"""Tests for the narration pipeline pieces that regressed during recent
prompt/UX iteration. Each test corresponds to a bug we shipped a fix for.
"""

from __future__ import annotations

import pytest

from contracts.schemas import ChunkNarration, NarrationSegment, CodeAnchor
from pr_walkthrough.llm.adapter import ClaudeLLMAdapter, _coerce_anchors
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
