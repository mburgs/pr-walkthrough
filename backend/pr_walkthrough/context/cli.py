"""CLI entry-point for context retrieval.

Usage
-----
    python -m pr_walkthrough.context.cli <repo-root> <file>:<start>-<end>

Example
-------
    python -m pr_walkthrough.context.cli \\
        backend/context/tests/fixtures/sample_repo \\
        sample_repo/store.py:10-15

Prints a JSON array of RelatedCode objects to stdout.
Exit code 1 on any error (bad arguments, missing ripgrep, etc.).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path


_ANCHOR_RE = re.compile(r"^(.+):(\d+)-(\d+)$")


def _parse_anchor(raw: str) -> tuple[str, int, int]:
    m = _ANCHOR_RE.match(raw)
    if not m:
        raise ValueError(
            f"Invalid anchor format {raw!r}. Expected <file>:<start>-<end>"
        )
    file_part = m.group(1)
    start = int(m.group(2))
    end = int(m.group(3))
    if start > end:
        raise ValueError(f"start ({start}) must be <= end ({end})")
    return file_part, start, end


async def _main(repo_root_str: str, anchor_str: str) -> int:
    # Import contracts — try repo-root-relative contracts package first.
    # In the project the contracts/ dir is at the repo root (two levels up from
    # backend/pr_walkthrough/context).  When running from within the backend/
    # directory, add the repo root to sys.path so `from contracts.schemas import
    # ...` works.
    script_dir = Path(__file__).resolve().parent  # .../backend/pr_walkthrough/context
    repo_root_candidate = script_dir.parent.parent.parent  # .../backend/../..
    if str(repo_root_candidate) not in sys.path:
        sys.path.insert(0, str(repo_root_candidate))

    try:
        from contracts.schemas import CodeAnchor  # type: ignore
    except ModuleNotFoundError as exc:
        print(f"ERROR: cannot import contracts: {exc}", file=sys.stderr)
        print(
            "Make sure you run this from the repo root or have contracts/ on PYTHONPATH.",
            file=sys.stderr,
        )
        return 1

    from pr_walkthrough.context.retriever import (  # type: ignore
        RipgrepContextRetriever,
        RipgrepNotFoundError,
    )

    repo_root = Path(repo_root_str).resolve()
    if not repo_root.is_dir():
        print(f"ERROR: repo-root is not a directory: {repo_root}", file=sys.stderr)
        return 1

    try:
        file_part, start, end = _parse_anchor(anchor_str)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    anchor = CodeAnchor(file=file_part, line_range=(start, end))
    retriever = RipgrepContextRetriever()

    try:
        results = await retriever.related(anchor, repo_root)
    except RipgrepNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Serialize using Pydantic model_dump
    output = [r.model_dump() for r in results]
    print(json.dumps(output, indent=2))
    return 0


def main() -> None:
    if len(sys.argv) != 3:
        print(
            "Usage: python -m pr_walkthrough.context.cli <repo-root> <file>:<start>-<end>",
            file=sys.stderr,
        )
        sys.exit(1)

    repo_root_str = sys.argv[1]
    anchor_str = sys.argv[2]

    exit_code = asyncio.run(_main(repo_root_str, anchor_str))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
