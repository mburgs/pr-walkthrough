"""RipgrepContextRetriever — implements ContextRetriever protocol.

Design decisions:
- Language-agnostic: no per-language code paths.  Classification uses file
  extension hints for definition patterns where unambiguous, but falls back
  to common heuristics.
- No LSP, no embeddings, no AI ranking.  Boring and fast.
- Async via asyncio.create_subprocess_exec; never blocks the event loop.
- Lines are read lazily (seeked by byte offset from a pre-built line-offset
  index) so entire files are never held in memory.
- prior_version is not implemented in v1; it would require `git log -S` or
  `git log -p`.  See README.md for the upgrade path.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Public re-export so callers do:
#   from pr_walkthrough.context.retriever import RipgrepContextRetriever
# ---------------------------------------------------------------------------

__all__ = ["RipgrepContextRetriever", "RipgrepNotFoundError"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RipgrepNotFoundError(RuntimeError):
    """Raised when the ``rg`` binary cannot be found on PATH."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RESULTS: Final[int] = 8
SNIPPET_CONTEXT_LINES: Final[int] = 5  # lines before + after match
MAX_LINE_CHARS: Final[int] = 120

# Relationship rank for sorting (lower == higher priority)
_RANK: Final[dict[str, int]] = {
    "definition": 0,
    "test": 1,
    "callsite": 2,
    "sibling": 3,
    "prior_version": 4,
}

# Patterns that indicate a definition, keyed by file extension.
# Checked in order; first match wins.
_DEFINITION_PATTERNS_GENERIC: Final[list[re.Pattern[str]]] = [
    re.compile(r"^\s*(async\s+)?def\s+\w"),          # Python / MicroPython
    re.compile(r"^\s*class\s+\w"),                    # Python / many langs
    re.compile(r"^\s*(export\s+)?(async\s+)?function\s+\w"),  # JS/TS
    re.compile(r"^\s*(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s*)?\("),  # arrow fn
    re.compile(r"^\s*(export\s+)?(interface|type)\s+\w"),      # TS
    re.compile(r"^\s*func\s+\w"),                     # Go
    re.compile(r"^\s*(pub\s+)?(async\s+)?fn\s+\w"),  # Rust
    re.compile(r"^\s*(public|private|protected|static|final)\s+[\w<>\[\]]+\s+\w+\s*\("),  # Java/C#
]

# Patterns that suggest "test file" by path
_TEST_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]+\.(py|js|ts|rb)$|_test\.(go|py|js|ts|rb)$|\.spec\.(js|ts|jsx|tsx)$|\.test\.(js|ts|jsx|tsx)$",
    re.IGNORECASE,
)

# Tokens to extract from anchor lines
_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")

# Words that are too generic to search for
_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "def", "class", "return", "import", "from", "if", "else", "elif",
        "for", "while", "with", "try", "except", "finally", "raise", "pass",
        "async", "await", "None", "True", "False", "self", "cls", "str",
        "int", "float", "bool", "list", "dict", "set", "tuple", "type",
        "None", "Optional", "Any", "Union", "const", "let", "var", "function",
        "interface", "export", "default", "return", "public", "private",
        "static", "void", "new", "this", "func", "struct", "impl", "use",
        "mod", "pub", "fn", "mut", "print", "super", "isinstance", "len",
        "range", "enumerate", "append", "extend", "get", "set", "items",
    }
)


# ---------------------------------------------------------------------------
# Lazy line-offset reader (avoids reading entire files into memory)
# ---------------------------------------------------------------------------


def _build_line_offsets(path: Path) -> list[int]:
    """Return byte offsets of each line start (0-indexed line numbers)."""
    offsets: list[int] = []
    with path.open("rb") as f:
        offsets.append(0)
        for line in f:
            offsets.append(offsets[-1] + len(line))
    return offsets


def _read_lines(path: Path, offsets: list[int], start: int, end: int) -> list[str]:
    """Read lines [start, end] inclusive (1-indexed). Returns decoded lines."""
    if start < 1:
        start = 1
    if end > len(offsets):
        end = len(offsets)
    if start > end:
        return []
    byte_start = offsets[start - 1]
    byte_end = offsets[min(end, len(offsets) - 1)]
    with path.open("rb") as f:
        f.seek(byte_start)
        raw = f.read(byte_end - byte_start)
    return raw.decode("utf-8", errors="replace").splitlines()


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------


def _extract_symbols(lines: list[str]) -> list[str]:
    """Extract candidate symbol names from anchor lines, ranked by frequency."""
    freq: dict[str, int] = {}
    for line in lines:
        for m in _SYMBOL_RE.finditer(line):
            tok = m.group(1)
            if tok not in _STOP_WORDS:
                freq[tok] = freq.get(tok, 0) + 1
    # Sort by: frequency desc, then length desc (longer = more specific), then alpha
    ranked = sorted(freq.keys(), key=lambda t: (-freq[t], -len(t), t))
    return ranked[:3]  # top 3 candidates


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _is_test_path(file_path: str) -> bool:
    return bool(_TEST_PATH_RE.search(file_path))


def _is_definition(line: str) -> bool:
    for pat in _DEFINITION_PATTERNS_GENERIC:
        if pat.search(line):
            return True
    return False


def _classify_hit(
    hit_file: str,
    hit_line: str,
    anchor_file: str,
    anchor_start: int,
    anchor_end: int,
    hit_lineno: int,
) -> str:
    """Return the RelationshipKind string for a ripgrep hit."""
    if _is_test_path(hit_file):
        return "test"
    if hit_file == anchor_file:
        # Same file but outside the anchor range → sibling
        if hit_lineno < anchor_start or hit_lineno > anchor_end:
            return "sibling"
        # Inside anchor range — skip
        return "__skip__"
    if _is_definition(hit_line):
        return "definition"
    return "callsite"


# ---------------------------------------------------------------------------
# Snippet extraction
# ---------------------------------------------------------------------------


def _build_snippet(
    path: Path,
    offsets: list[int],
    match_lineno: int,
    context: int = SNIPPET_CONTEXT_LINES,
    max_chars: int = MAX_LINE_CHARS,
) -> tuple[int, int, str]:
    """Return (start_line, end_line, snippet_text)."""
    total_lines = len(offsets)
    start = max(1, match_lineno - context)
    end = min(total_lines, match_lineno + context)
    lines = _read_lines(path, offsets, start, end)
    truncated = [line[:max_chars] for line in lines]
    return start, end, "\n".join(truncated)


# ---------------------------------------------------------------------------
# Ripgrep runner
# ---------------------------------------------------------------------------


async def _run_rg(symbol: str, repo_root: Path) -> list[dict]:
    """Run ``rg --json -n -w <symbol>`` and return parsed JSON objects."""
    cmd = [
        "rg",
        "--json",
        "-n",          # line numbers
        "-w",          # whole-word match
        "--max-count", "40",  # don't explode on very common tokens
        symbol,
        str(repo_root),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RipgrepNotFoundError(
            "ripgrep ('rg') not found on PATH. "
            "Install it: https://github.com/BurntSushi/ripgrep#installation"
        )

    stdout, _stderr = await proc.communicate()

    results: list[dict] = []
    for raw_line in stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "match":
            results.append(obj)
    return results


# ---------------------------------------------------------------------------
# Main retriever class
# ---------------------------------------------------------------------------


class RipgrepContextRetriever:
    """Implements the ContextRetriever protocol using ripgrep.

    Entirely local — zero network calls.

    LSP enrichment note
    -------------------
    This implementation uses text search only.  LSP would add:
    - Precise go-to-definition (resolves imports, handles overloads).
    - Find-all-references across the whole workspace without heuristic
      classification.
    - Type-aware callsite filtering (call vs. attribute access).
    The upgrade path is to run an LSP server (e.g. pylsp, gopls) in a
    subprocess and use the Language Server Protocol ``textDocument/definition``
    and ``textDocument/references`` JSON-RPC calls as a second pass to
    re-classify or supplement ripgrep hits.  The ripgrep pass stays as a
    fast pre-filter for languages without an LSP.
    """

    async def related(
        self,
        anchor: "CodeAnchor",  # noqa: F821 — imported at call site
        repo_root: Path,
    ) -> "list[RelatedCode]":  # noqa: F821
        from contracts.schemas import CodeAnchor as _CA, RelatedCode as _RC  # type: ignore

        anchor_file = anchor.file
        anchor_start, anchor_end = anchor.line_range

        # Resolve anchor file path relative to repo_root
        anchor_path = repo_root / anchor_file
        if not anchor_path.exists():
            return []

        # 1. Read anchor lines
        offsets = _build_line_offsets(anchor_path)
        anchor_lines = _read_lines(offsets=offsets, path=anchor_path,
                                   start=anchor_start, end=anchor_end)

        # 2. Extract symbols
        symbols = _extract_symbols(anchor_lines)
        if not symbols:
            return []

        # 3. Search for each symbol, deduplicate by (file, lineno)
        seen: set[tuple[str, int]] = set()
        candidates: list[dict] = []  # {file, lineno, line_text, relationship, offset_cache}

        for symbol in symbols:
            hits = await _run_rg(symbol, repo_root)
            for hit in hits:
                data = hit["data"]
                abs_path = data["path"]["text"]
                rel_path = str(Path(abs_path).relative_to(repo_root))
                lineno = data["line_number"]
                line_text = data["lines"]["text"].rstrip("\n")

                key = (rel_path, lineno)
                if key in seen:
                    continue
                seen.add(key)

                rel = _classify_hit(
                    hit_file=rel_path,
                    hit_line=line_text,
                    anchor_file=anchor_file,
                    anchor_start=anchor_start,
                    anchor_end=anchor_end,
                    hit_lineno=lineno,
                )
                if rel == "__skip__":
                    continue

                candidates.append({
                    "file": rel_path,
                    "lineno": lineno,
                    "line_text": line_text,
                    "relationship": rel,
                })

        # 4. Rank and cap
        candidates.sort(key=lambda c: (_RANK.get(c["relationship"], 99), c["file"], c["lineno"]))
        candidates = candidates[:MAX_RESULTS]

        # 5. Build RelatedCode objects with snippets
        # Cache line offsets per file to avoid re-reading
        offset_cache: dict[str, list[int]] = {}
        results: list[_RC] = []

        for cand in candidates:
            file_path = repo_root / cand["file"]
            if not file_path.exists():
                continue
            if cand["file"] not in offset_cache:
                offset_cache[cand["file"]] = _build_line_offsets(file_path)
            file_offsets = offset_cache[cand["file"]]
            snip_start, snip_end, snippet = _build_snippet(
                path=file_path,
                offsets=file_offsets,
                match_lineno=cand["lineno"],
            )
            results.append(
                _RC(
                    anchor=_CA(file=cand["file"], line_range=(snip_start, snip_end)),
                    relationship=cand["relationship"],
                    snippet=snippet,
                )
            )

        return results
