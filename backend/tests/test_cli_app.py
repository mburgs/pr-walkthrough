"""CLI entry-point helpers: PR-ref parsing + log filter."""

from __future__ import annotations

import io
import stat
from pathlib import Path

import pytest

from pr_walkthrough.cli_app import (
    _PROGRESS_PATTERNS,
    _find_venv_bin,
    _format_template,
    _forwarder,
    _is_error_line,
    _is_suppressed,
    _is_traceback_continuation,
    _parse_args,
    _strip_log_prefix,
    main,
    parse_pr_ref,
    pick_port,
    repo_root_from_args,
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


# --------------------------------------------------------------------------- _parse_args


def test_parse_args_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "PR_WALKTHROUGH_PORT", "PR_WALKTHROUGH_FRONTEND_PORT", "PR_WALKTHROUGH_REPOS_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    args = _parse_args(["owner/repo/pull/1"])
    assert args.pr == "owner/repo/pull/1"
    assert args.familiarity is None
    assert args.port is None
    assert args.frontend_port is None
    assert args.repos_dir == Path.home() / "code"
    assert args.no_open is False


def test_parse_args_flags_override_defaults(tmp_path: Path) -> None:
    args = _parse_args([
        "owner/repo/pull/1",
        "--familiarity", "tour",
        "--port", "9001",
        "--frontend-port", "9002",
        "--repos-dir", str(tmp_path),
        "--no-open",
    ])
    assert args.familiarity == "tour"
    assert args.port == 9001
    assert args.frontend_port == 9002
    assert args.repos_dir == tmp_path
    assert args.no_open is True


def test_parse_args_reads_ports_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PR_WALKTHROUGH_PORT", "5555")
    monkeypatch.setenv("PR_WALKTHROUGH_FRONTEND_PORT", "5556")
    args = _parse_args(["owner/repo/pull/1"])
    assert args.port == 5555
    assert args.frontend_port == 5556


def test_parse_args_rejects_invalid_familiarity() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["owner/repo/pull/1", "--familiarity", "bogus"])


# --------------------------------------------------------------------------- repo_root_from_args


def test_repo_root_from_args_uses_matching_subdir_when_present(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    args = _parse_args(["owner/repo/pull/1", "--repos-dir", str(tmp_path)])
    root = repo_root_from_args(args, "https://github.com/owner/repo/pull/1")
    assert root == tmp_path / "repo"


def test_repo_root_from_args_falls_back_to_repos_dir_when_absent(tmp_path: Path) -> None:
    args = _parse_args(["owner/repo/pull/1", "--repos-dir", str(tmp_path)])
    root = repo_root_from_args(args, "https://github.com/owner/repo/pull/1")
    assert root == tmp_path


# --------------------------------------------------------------------------- _find_venv_bin


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_find_venv_bin_prefers_main_repo_venv(tmp_path: Path) -> None:
    _make_executable(tmp_path / ".venv" / "bin" / "uvicorn")
    assert _find_venv_bin(tmp_path, "uvicorn") == str(tmp_path / ".venv" / "bin" / "uvicorn")


def test_find_venv_bin_falls_back_to_worktree_venv(tmp_path: Path) -> None:
    exe = tmp_path / ".claude" / "worktrees" / "feature-x" / ".venv" / "bin" / "uvicorn"
    _make_executable(exe)
    assert _find_venv_bin(tmp_path, "uvicorn") == str(exe)


def test_find_venv_bin_falls_back_to_bare_name_when_nothing_found(tmp_path: Path) -> None:
    assert _find_venv_bin(tmp_path, "uvicorn") == "uvicorn"


def test_find_venv_bin_ignores_non_executable_file(tmp_path: Path) -> None:
    path = tmp_path / ".venv" / "bin" / "uvicorn"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")  # not chmod +x
    assert _find_venv_bin(tmp_path, "uvicorn") == "uvicorn"


# --------------------------------------------------------------------------- _strip_log_prefix


def test_strip_log_prefix_strips_uvicorn_style() -> None:
    assert _strip_log_prefix("INFO:     Uvicorn running on http://127.0.0.1:8000") == (
        "Uvicorn running on http://127.0.0.1:8000"
    )


def test_strip_log_prefix_strips_timestamped_logger_line() -> None:
    line = "2026-01-01 12:00:00,123 pr_walkthrough.main INFO chunk ready"
    assert _strip_log_prefix(line) == "chunk ready"


def test_strip_log_prefix_leaves_unrecognized_lines_untouched() -> None:
    assert _strip_log_prefix("just a plain line") == "just a plain line"


# --------------------------------------------------------------------------- pick_port


def test_pick_port_returns_a_bindable_port() -> None:
    import socket

    port = pick_port()
    assert isinstance(port, int)
    assert 1024 < port < 65536
    # The port was released after pick_port() returned, so it must be
    # possible to bind it again immediately.
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


# --------------------------------------------------------------------------- main() early exits


def test_main_returns_2_for_unparsable_pr_ref(capsys: pytest.CaptureFixture) -> None:
    assert main(["not-a-pr-ref"]) == 2
    assert "error:" in capsys.readouterr().err


def test_main_returns_2_when_no_pr_ref_and_non_interactive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main([]) == 2
    assert "PR ref required" in capsys.readouterr().err


def test_main_rejects_invalid_familiarity_via_argparse() -> None:
    with pytest.raises(SystemExit):
        main(["owner/repo/pull/1", "--familiarity", "nonsense"])
