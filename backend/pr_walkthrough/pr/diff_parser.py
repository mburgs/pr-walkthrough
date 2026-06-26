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

# `diff --git` is the file boundary, but its path parsing is unreliable when
# filenames contain spaces or special chars — git emits a/"some file.py"
# "b/some file.py" in that case, and the unquoted form `a/foo b/bar`
# matches a greedy `.+` ambiguously. The `+++ b/` line is the source of
# truth for the new-side path, so we only use _DIFF_HEADER as a *boundary
# marker* and trust _NEW_FILE for the path.
_DIFF_HEADER = re.compile(r"^diff --git ")
_NEW_FILE = re.compile(r'^\+\+\+ "?b/(.+?)"?$')
_OLD_FILE = re.compile(r'^--- "?a/(.+?)"?$')
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
        # New file in diff. The +++ line that follows sets the canonical
        # path; clear state here so a missing +++ (deleted file) leaves
        # current_file None and the hunk gets dropped from `flush()` rather
        # than mis-attributed to the previous file.
        if _DIFF_HEADER.match(line):
            flush()
            body_lines = []
            current_header = None
            current_old_range = None
            current_new_range = None
            current_file = None
            continue

        new_file_match = _NEW_FILE.match(line)
        if new_file_match:
            current_file = new_file_match.group(1)
            continue

        if _NEW_FILE_DEVNULL.match(line):
            # Deleted file — the +++ side is /dev/null, so fall back to whatever
            # the --- side parsed (set just below). Leave current_file as the
            # old-side path if it was captured.
            continue

        # --- line: usually the old-side path. We only need it for deleted
        # files (so they get attributed to the path that *used* to exist).
        # For modify/rename, +++ overrides this.
        old_match = _OLD_FILE.match(line)
        if old_match:
            current_file = old_match.group(1)
            continue
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
