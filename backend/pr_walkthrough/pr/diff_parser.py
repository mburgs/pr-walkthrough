"""Parse unified diff text into list[Hunk].

Handles standard unified diff format as produced by `gh pr diff`:
  diff --git a/foo.py b/foo.py
  --- a/foo.py
  +++ b/foo.py
  @@ -42,12 +42,28 @@ class Foo:
  <body lines>

Each hunk gets:
  file      – path from +++ header (strips the b/ prefix)
  old_range – (start, count) from @@ -start,count
  new_range – (start, count) from @@ +start,count
  header    – the full @@ … @@ line
  body      – raw lines with +/-/space prefixes, joined with \n
"""

from __future__ import annotations

import re
from sys import intern

from contracts.schemas import Hunk

_DIFF_HEADER = re.compile(r"^diff --git a/.+ b/(.+)$")
_NEW_FILE = re.compile(r"^\+\+\+ b/(.+)$")
_NEW_FILE_DEVNULL = re.compile(r"^\+\+\+ /dev/null$")
_HUNK_HEADER = re.compile(
    r"^(@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:\s.*)?)$"
)


def parse_unified_diff(text: str) -> list[Hunk]:
    """Parse a unified diff string into a list of Hunk objects."""
    hunks: list[Hunk] = []
    current_file: str | None = None
    current_header: str | None = None
    current_old_range: tuple[int, int] | None = None
    current_new_range: tuple[int, int] | None = None
    body_lines: list[str] = []

    def flush() -> None:
        if current_file and current_header is not None and current_old_range is not None:
            # Join body lines; add trailing newline to mirror unified diff convention
            # (each line in a diff ends with \n, so the full body ends with \n too)
            raw_body = "\n".join(body_lines)
            if body_lines:
                raw_body += "\n"
            hunks.append(
                Hunk(
                    file=current_file,
                    old_range=current_old_range,
                    new_range=current_new_range,  # type: ignore[arg-type]
                    header=current_header,
                    body=raw_body,
                )
            )

    for line in text.splitlines():
        # New file in diff
        diff_match = _DIFF_HEADER.match(line)
        if diff_match:
            flush()
            body_lines = []
            current_header = None
            current_old_range = None
            current_new_range = None
            # file will be set by +++ line; grab tentatively
            current_file = diff_match.group(1)
            continue

        new_file_match = _NEW_FILE.match(line)
        if new_file_match:
            current_file = new_file_match.group(1)
            continue

        if _NEW_FILE_DEVNULL.match(line):
            # deleted file — keep current_file from diff --git line
            continue

        # --- line: skip (old file)
        if line.startswith("--- "):
            continue

        hunk_match = _HUNK_HEADER.match(line)
        if hunk_match:
            flush()
            body_lines = []
            full_header = hunk_match.group(1).rstrip()
            old_start = int(hunk_match.group(2))
            old_count = int(hunk_match.group(3)) if hunk_match.group(3) is not None else 1
            new_start = int(hunk_match.group(4))
            new_count = int(hunk_match.group(5)) if hunk_match.group(5) is not None else 1
            current_header = full_header
            current_old_range = (old_start, old_count)
            current_new_range = (new_start, new_count)
            continue

        # Body lines: +, -, space, and \\ (no newline at end of file)
        if current_header is not None and (
            line.startswith(("+", "-", " ", "\\"))
        ):
            body_lines.append(line)
            continue

        # index, new mode, etc. — ignore
    flush()
    return hunks
