"""Tests for the unified diff parser.

Strategy:
  1. Load fixtures/pr_small/diff.json — the ground-truth Hunk list.
  2. Reconstruct a minimal unified diff string from each Hunk.
  3. Re-parse the reconstructed diff.
  4. Assert the round-trip produces identical Hunk objects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.schemas import Hunk
from pr_walkthrough.pr.diff_parser import parse_unified_diff

FIXTURES_DIR = Path(__file__).parents[3] / "fixtures" / "pr_small"


def _load_fixture_hunks() -> list[Hunk]:
    data = json.loads((FIXTURES_DIR / "diff.json").read_text())
    return [Hunk(**h) for h in data]


def _hunk_to_diff_text(hunk: Hunk) -> str:
    """Reconstruct a minimal unified diff fragment from a Hunk."""
    old_start, old_count = hunk.old_range
    new_start, new_count = hunk.new_range
    lines = [
        f"diff --git a/{hunk.file} b/{hunk.file}",
        f"--- a/{hunk.file}",
        f"+++ b/{hunk.file}",
        hunk.header,
        hunk.body,
    ]
    return "\n".join(lines)


class TestDiffParserFixture:
    def test_fixture_file_exists(self) -> None:
        assert (FIXTURES_DIR / "diff.json").exists()

    def test_parse_returns_three_hunks(self) -> None:
        hunks = _load_fixture_hunks()
        assert len(hunks) == 3

    def test_round_trip_each_hunk(self) -> None:
        """Parse → reconstruct → re-parse → compare each hunk."""
        fixture_hunks = _load_fixture_hunks()
        for original in fixture_hunks:
            diff_text = _hunk_to_diff_text(original)
            reparsed = parse_unified_diff(diff_text)
            assert len(reparsed) == 1, (
                f"Expected exactly 1 hunk from re-parse, got {len(reparsed)}"
            )
            got = reparsed[0]
            assert got.file == original.file
            assert got.old_range == original.old_range
            assert got.new_range == original.new_range
            assert got.header == original.header
            assert got.body == original.body

    def test_round_trip_full_fixture(self) -> None:
        """Reconstruct the entire diff from all fixture hunks and re-parse."""
        fixture_hunks = _load_fixture_hunks()
        # Build a full diff with all three files
        sections: list[str] = []
        for hunk in fixture_hunks:
            sections.append(
                "\n".join([
                    f"diff --git a/{hunk.file} b/{hunk.file}",
                    f"--- a/{hunk.file}",
                    f"+++ b/{hunk.file}",
                    hunk.header,
                    hunk.body,
                ])
            )
        full_diff = "\n".join(sections)
        reparsed = parse_unified_diff(full_diff)
        assert len(reparsed) == len(fixture_hunks)
        for original, got in zip(fixture_hunks, reparsed):
            assert got.file == original.file
            assert got.old_range == original.old_range
            assert got.new_range == original.new_range
            assert got.header == original.header
            assert got.body == original.body


class TestDiffParserEdgeCases:
    def test_empty_input(self) -> None:
        assert parse_unified_diff("") == []

    def test_added_file(self) -> None:
        """A diff with old_range (0,0) — new file."""
        diff = (
            "diff --git a/new.py b/new.py\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+line1\n"
            "+line2\n"
            "+line3\n"
        )
        hunks = parse_unified_diff(diff)
        assert len(hunks) == 1
        assert hunks[0].file == "new.py"
        assert hunks[0].old_range == (0, 0)
        assert hunks[0].new_range == (1, 3)

    def test_hunk_with_no_count(self) -> None:
        """@@ -5 +5 @@ — count defaults to 1."""
        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -5 +5 @@\n"
            " unchanged\n"
        )
        hunks = parse_unified_diff(diff)
        assert hunks[0].old_range == (5, 1)
        assert hunks[0].new_range == (5, 1)

    def test_multiple_hunks_in_one_file(self) -> None:
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,2 +1,2 @@ first\n"
            "-old\n"
            "+new\n"
            "@@ -10,2 +10,3 @@ second\n"
            " ctx\n"
            "+extra\n"
            " ctx2\n"
        )
        hunks = parse_unified_diff(diff)
        assert len(hunks) == 2
        assert hunks[0].old_range == (1, 2)
        assert hunks[1].old_range == (10, 2)
        assert hunks[0].file == "foo.py"
        assert hunks[1].file == "foo.py"

    def test_hunk_body_preserves_prefixes(self) -> None:
        """Body lines must keep their +/-/space prefixes."""
        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,3 +1,3 @@\n"
            " context\n"
            "-removed\n"
            "+added\n"
        )
        hunks = parse_unified_diff(diff)
        body = hunks[0].body
        assert " context" in body
        assert "-removed" in body
        assert "+added" in body

    def test_function_context_in_header(self) -> None:
        diff = (
            "diff --git a/auth.py b/auth.py\n"
            "--- a/auth.py\n"
            "+++ b/auth.py\n"
            "@@ -42,12 +42,28 @@ class SessionStore:\n"
            "-old\n"
            "+new\n"
        )
        hunks = parse_unified_diff(diff)
        assert hunks[0].header == "@@ -42,12 +42,28 @@ class SessionStore:"

    def test_path_with_spaces(self) -> None:
        """`gh pr diff` quotes paths that contain spaces — the parser must
        unquote the +++ line rather than dropping the hunk silently."""
        diff = (
            'diff --git "a/some file.py" "b/some file.py"\n'
            '--- "a/some file.py"\n'
            '+++ "b/some file.py"\n'
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        hunks = parse_unified_diff(diff)
        assert len(hunks) == 1
        assert hunks[0].file == "some file.py"

    def test_deleted_file_attributed_to_old_path(self) -> None:
        """+++ /dev/null means the file was deleted; the hunk should still
        carry the path from --- a/<path>, not be silently dropped."""
        diff = (
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-line1\n"
            "-line2\n"
        )
        hunks = parse_unified_diff(diff)
        assert len(hunks) == 1
        assert hunks[0].file == "gone.py"
        assert hunks[0].new_range == (0, 0)
