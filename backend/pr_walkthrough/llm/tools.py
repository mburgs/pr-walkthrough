"""Retrieval tools the follow-up Q&A loop can call.

Two tools are exposed to the model alongside the structured-output
`emit_follow_up_answer` tool:

- `read_file_lines` — slice a range from a file in the PR's repo.
- `grep_repo` — search the repo with ripgrep for a regex.

Both are read-only. Path traversal is refused (resolved path must stay
under `repo_root`). Errors are returned as `"ERROR: <reason>"` strings
rather than raised — the adapter ships them back to the model as tool
results so it can recover (e.g. fix a bad path) instead of the request
dying mid-loop.

Caps:

- `read_file_lines`: max 200 lines per call. The model's input range is
  clamped server-side, not validated against — the model sometimes asks
  for a thousand-line span "just in case" and we'd rather silently trim
  than reject the call.
- `grep_repo`: max_results capped at 100; combined output truncated at
  ~8000 chars to keep tool-result payloads small.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

# Tool schemas (Anthropic tool-use format) ---------------------------------

READ_FILE_LINES_TOOL: dict[str, Any] = {
    "name": "read_file_lines",
    "description": (
        "Read a range of lines from a file in the PR's repo. Use this to "
        "inspect code referenced by the diff but not contained in it "
        "(helpers, callers, related types). Returns plain text with line "
        "numbers prefixed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Repo-relative path to the file.",
            },
            "start": {
                "type": "integer",
                "description": "1-indexed first line (inclusive).",
            },
            "end": {
                "type": "integer",
                "description": (
                    "1-indexed last line (inclusive). The call returns at "
                    "most 200 lines; larger ranges are clamped silently."
                ),
            },
        },
        "required": ["path", "start", "end"],
        "additionalProperties": False,
    },
}

GREP_REPO_TOOL: dict[str, Any] = {
    "name": "grep_repo",
    "description": (
        "Search the PR's repo with ripgrep for a regex pattern. Returns up "
        "to `max_results` matches as 'path:line:text' lines. Use this to "
        "find where a symbol is defined / used outside the diff."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Ripgrep-compatible regex.",
            },
            "path_glob": {
                "type": "string",
                "description": "Optional glob filter, e.g. '*.py'.",
            },
            "max_results": {
                "type": "integer",
                "description": (
                    "Cap on returned matches. Default 30; values above "
                    "100 are clamped to 100."
                ),
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
}


# Executors ----------------------------------------------------------------

_MAX_LINES = 200
_MAX_GREP_RESULTS = 100
_DEFAULT_GREP_RESULTS = 30
_MAX_GREP_OUTPUT_CHARS = 8000


def _resolve_under_root(repo_root: Path, rel: str) -> Path | None:
    """Resolve `rel` under `repo_root`. Return None on traversal escape.

    Resolves both sides so symlinks and `..` segments can't smuggle
    access outside the repo.
    """
    root = repo_root.resolve()
    try:
        candidate = (root / rel).resolve()
    except Exception:
        return None
    if not str(candidate).startswith(str(root)):
        return None
    return candidate


def execute_read_file_lines(args: dict, repo_root: Path) -> str:
    """Return the requested [start, end] slice of `path`, line-prefixed.

    Errors come back as `"ERROR: <reason>"` so the model can self-correct
    on the next tool round.
    """
    try:
        path = str(args["path"])
        start = int(args["start"])
        end = int(args["end"])
    except (KeyError, TypeError, ValueError) as e:
        return f"ERROR: bad arguments: {e}"

    if start < 1:
        start = 1
    if end < start:
        return f"ERROR: end ({end}) is before start ({start})"
    # Silently clamp wide ranges; see module docstring.
    end = min(end, start + _MAX_LINES - 1)

    resolved = _resolve_under_root(repo_root, path)
    if resolved is None:
        return f"ERROR: path {path!r} escapes repo root"
    if not resolved.is_file():
        return f"ERROR: {path!r} is not a file"
    try:
        text = resolved.read_text(errors="replace")
    except Exception as e:
        return f"ERROR: failed to read {path!r}: {e}"
    lines = text.splitlines()
    if start > len(lines):
        return f"ERROR: start line {start} is past EOF (file has {len(lines)} lines)"
    selected = lines[start - 1 : end]
    return "\n".join(f"L{n:>5}  {line}" for n, line in enumerate(selected, start))


def execute_grep_repo(args: dict, repo_root: Path) -> str:
    """Run `rg` in `repo_root` and return matches as plain text.

    No shell — args are passed directly to subprocess. Empty result is
    returned as the literal string '(no matches)' so the model gets a
    clear signal instead of an empty tool_result.
    """
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return "ERROR: 'pattern' is required"
    path_glob = args.get("path_glob")
    raw_max = args.get("max_results", _DEFAULT_GREP_RESULTS)
    try:
        max_results = int(raw_max)
    except (TypeError, ValueError):
        max_results = _DEFAULT_GREP_RESULTS
    max_results = max(1, min(_MAX_GREP_RESULTS, max_results))

    rg = shutil.which("rg")
    if rg is None:
        return "ERROR: ripgrep ('rg') not installed on the server"

    cmd = [rg, "--line-number", "--color=never", "-m", str(max_results), pattern]
    if isinstance(path_glob, str) and path_glob:
        cmd.extend(["--glob", path_glob])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return "ERROR: grep timed out"
    except Exception as e:
        return f"ERROR: grep failed: {e}"

    # rg exits 1 with no output when there are no matches — not an error.
    if proc.returncode not in (0, 1):
        stderr = proc.stderr.strip() or f"exit {proc.returncode}"
        return f"ERROR: grep failed: {stderr}"

    out = proc.stdout
    if not out.strip():
        return "(no matches)"
    if len(out) > _MAX_GREP_OUTPUT_CHARS:
        out = out[:_MAX_GREP_OUTPUT_CHARS] + "\n… (truncated)"
    return out


def execute_tool(name: str, args: dict, repo_root: Path) -> str:
    """Dispatch to the right executor by tool name."""
    if name == "read_file_lines":
        return execute_read_file_lines(args, repo_root)
    if name == "grep_repo":
        return execute_grep_repo(args, repo_root)
    return f"ERROR: unknown tool {name!r}"
