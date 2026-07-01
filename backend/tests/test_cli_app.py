"""CLI entry-point helpers: PR-ref parsing + log filter."""

from __future__ import annotations

import io

import pytest

from pr_walkthrough.cli_app import (
    _PROGRESS_PATTERNS,
    _forwarder,
    _format_template,
    _is_error_line,
    _is_suppressed,
    _is_traceback_continuation,
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
    # `cache hit` now bubbles through as a status update so the user
    # can see the persistent cache is doing its job.
    assert not _is_suppressed("cache hit: sess_x/c1 (review)")
    assert not _is_suppressed("progress: fetching PR ...")


def test_traceback_continuation_detection() -> None:
    assert _is_traceback_continuation('  File "pool.py", line 69, in get')
    assert _is_traceback_continuation('    client = await LSPClient.spawn(cmd, cwd=repo_root)')
    assert _is_traceback_continuation("Traceback (most recent call last):")
    assert _is_traceback_continuation("During handling of the above exception, another exception occurred:")
    assert not _is_traceback_continuation("FileNotFoundError: [Errno 2] No such file or directory: 'pyright-langserver'")
    assert not _is_traceback_continuation("INFO  starting up")


def test_forwarder_prints_full_traceback_body(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression test: a multi-line backend traceback must reach the
    user's terminal in full, not just the "Traceback (most recent call
    last):" header. `_forwarder` reads the subprocess pipe one line at
    a time, and only the header contains an `_ERROR_HINTS` keyword —
    without traceback-aware state, the frames and the actual exception
    message were silently dropped in non-verbose mode."""
    log_lines = [
        b"spawning LSP server pyright-langserver for python in /repo\n",
        b"Traceback (most recent call last):\n",
        b'  File "pool.py", line 69, in get\n',
        b"    client = await LSPClient.spawn(cmd, cwd=repo_root)\n",
        b'  File "client.py", line 56, in spawn\n',
        b"    proc = await asyncio.create_subprocess_exec(\n",
        b"FileNotFoundError: [Errno 2] No such file or directory: 'pyright-langserver'\n",
        b"INFO  next chunk starting\n",
    ]
    stream = io.BytesIO(b"".join(log_lines))
    _forwarder(stream, verbose=False)
    err = capsys.readouterr().err
    assert "Traceback (most recent call last):" in err
    assert 'File "pool.py", line 69, in get' in err
    assert "client = await LSPClient.spawn(cmd, cwd=repo_root)" in err
    assert "FileNotFoundError: [Errno 2] No such file or directory: 'pyright-langserver'" in err


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
