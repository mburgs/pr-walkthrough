"""CLI entry-point helpers: PR-ref parsing + log filter."""

from __future__ import annotations

import pytest

from pr_walkthrough.cli_app import (
    _is_error_line,
    _is_surface_line,
    parse_pr_ref,
)


@pytest.mark.parametrize(
    "input_,expected",
    [
        ("anthropics/claude/pull/42",
         "https://github.com/anthropics/claude/pull/42"),
        ("anthropics/claude/pull/42/",
         "https://github.com/anthropics/claude/pull/42"),
        ("https://github.com/anthropics/claude/pull/42",
         "https://github.com/anthropics/claude/pull/42"),
        ("https://www.github.com/anthropics/claude/pull/42",
         "https://github.com/anthropics/claude/pull/42"),
        ("https://github.com/anthropics/claude/pull/42/files",
         "https://github.com/anthropics/claude/pull/42"),
        ("  anthropics/claude/pull/42  ",
         "https://github.com/anthropics/claude/pull/42"),
    ],
)
def test_parse_pr_ref_accepts_both_forms(input_: str, expected: str) -> None:
    assert parse_pr_ref(input_) == expected


@pytest.mark.parametrize(
    "junk",
    [
        "not a pr",
        "anthropics/claude",  # no /pull/N
        "https://gitlab.com/foo/bar/pull/1",  # not github
        "owner/repo/issues/42",  # issue, not pull
        "",
    ],
)
def test_parse_pr_ref_rejects_garbage(junk: str) -> None:
    with pytest.raises(ValueError):
        parse_pr_ref(junk)


def test_error_line_detection() -> None:
    assert _is_error_line("Traceback (most recent call last):")
    assert _is_error_line("CRITICAL: it died")
    assert _is_error_line("ERROR processing chunk")
    assert _is_error_line('  File "foo.py", line 1, in main\n    raise Exception("x")')
    assert not _is_error_line("INFO  starting up")


def test_surface_line_detection() -> None:
    assert _is_surface_line("Uvicorn running on http://127.0.0.1:8000")
    assert _is_surface_line("VITE v8.1.0 dev server running")
    assert _is_surface_line("ready in 412 ms")
    assert _is_surface_line("cache hit: sess_x/c1 (review)")
    assert not _is_surface_line("INFO  some random log")
