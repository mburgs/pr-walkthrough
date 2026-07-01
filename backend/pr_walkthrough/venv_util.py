"""Locating the project's venv/tooling from source layout.

Shared by `cli_app` (launching the backend/frontend) and `setup_cmd`
(installing optional extras into the same venv) so both agree on where
`pip`, `uvicorn`, etc. live.
"""

from __future__ import annotations

import os
from pathlib import Path


def find_venv_bin(repo_root: Path, exe: str) -> str:
    """Find an installed binary in a `.venv` — main repo first, then any
    sibling worktree's venv (so a shared venv across worktrees works).
    Falls back to the bare binary name (PATH lookup) if no venv found."""
    candidates = [repo_root / ".venv" / "bin" / exe]
    candidates.extend(p / exe for p in (repo_root / ".claude" / "worktrees").glob("*/.venv/bin"))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return exe


def project_dirs(source_file: Path) -> tuple[Path, Path, Path]:
    """Given a file living at backend/pr_walkthrough/<name>.py, return
    (project_root, backend_dir, frontend_dir)."""
    backend_dir = source_file.resolve().parent.parent
    project_root = backend_dir.parent
    frontend_dir = project_root / "frontend"
    return project_root, backend_dir, frontend_dir
