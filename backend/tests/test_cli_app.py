"""CLI entry-point helpers: PR-ref parsing + log filter."""

from __future__ import annotations

import pytest

from pr_walkthrough.cli_app import (
    _PROGRESS_PATTERNS,
    _format_template,
    _is_error_line,
    _is_suppressed,
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


def test_suppressed_boot_noise() -> None:
    """Uvicorn / Vite startup lines are silenced because the CLI emits
    its own ✓ ready markers for those milestones."""
    assert _is_suppressed("Uvicorn running on http://127.0.0.1:8000")
    assert _is_suppressed("VITE v8.1.0 dev server running")
    assert _is_suppressed("ready in 412 ms")
    assert _is_suppressed("Application startup complete")
    assert _is_suppressed("cache hit: sess_x/c1 (review)")
    assert not _is_suppressed("progress: fetching PR ...")


def test_progress_markers_format_cleanly() -> None:
    """Backend `log.info("progress: ...")` lines must match a pattern and
    interpolate any capture group into the CLI-voice template."""
    import re
    cases = [
        ("progress: PR fetched (3 files, 5 hunks)", "PR fetched (3 files, 5 hunks)"),
        ("progress: tour ready (4 chunks)", "tour ready (4 chunks)"),
        ("progress: fetching PR https://github.com/x/y/pull/1", "fetching PR diff"),
        ("retriever: LSP for python (precise references)", "LSP active for python"),
    ]
    for line, expected in cases:
        for pat, _kind, tmpl in _PROGRESS_PATTERNS:
            m = pat.search(line)
            if m is None:
                continue
            assert _format_template(tmpl, m) == expected, line
            break
        else:
            raise AssertionError(f"no progress pattern matched: {line!r}")
