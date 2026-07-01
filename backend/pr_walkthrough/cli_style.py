"""Shared ANSI styling + status-line helpers for CLI output.

Used by both `cli_app` (the launcher) and `setup_cmd` (`pr-walkthrough
setup`) so the two commands read as one consistent voice. No-ops when
stdout isn't a TTY so piped / redirected output stays clean.
"""

from __future__ import annotations

import os
import sys


class Style:
    def __init__(self) -> None:
        self._on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._on else text

    def ok(self, t: str) -> str:    return self._wrap("32", t)           # green
    def warn(self, t: str) -> str:  return self._wrap("33", t)           # yellow
    def err(self, t: str) -> str:   return self._wrap("31", t)           # red
    def arrow(self, t: str) -> str: return self._wrap("36", t)           # cyan
    def dim(self, t: str) -> str:   return self._wrap("2", t)
    def bold(self, t: str) -> str:  return self._wrap("1", t)


S = Style()


def step(label: str) -> None:
    print(f"  {S.arrow('→')} {label}", flush=True)


def ok(label: str) -> None:
    print(f"  {S.ok('✓')} {label}", flush=True)


def warn(label: str) -> None:
    print(f"  {S.warn('!')} {label}", flush=True)


def err(label: str) -> None:
    print(f"  {S.err('✗')} {label}", file=sys.stderr, flush=True)


def section(label: str) -> None:
    print(f"\n{S.bold(label)}", flush=True)


def print_failure_detail(output: str, limit: int = 20) -> None:
    """Print the tail of captured subprocess output, dimmed, so a failed
    install is diagnosable without drowning the CLI in noise on the happy
    path. Only ever called on failure — successful commands stay silent."""
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    if not lines:
        return
    for line in lines[-limit:]:
        print(f"    {S.dim(line)}", file=sys.stderr, flush=True)
