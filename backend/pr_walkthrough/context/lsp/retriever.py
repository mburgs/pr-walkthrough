"""LSP-backed ContextRetriever.

Walks the identifiers in the anchor lines and asks the language server
for each one's definition + references. Filters:

  - **Skip import lines** entirely (`from X import Y` / `import X` /
    `import { Y } from "..."`). Imports surface their symbols in every
    file that uses them; including their identifiers as seeds biases
    related-code toward whatever happens to be imported here, not
    toward the actual change. (Concrete example: a Google adapter PR
    whose anchor line was `from core.models import ..., Transparency`
    would surface every `Transparency` site in the repo even though
    the change was about `organizer_email`.)
  - Skip keywords, common type names, and dunders.
  - Cap identifiers + results so a giant anchor doesn't explode the
    token budget downstream.

For each surviving identifier we call:
  textDocument/definition  — returns one location, classified as
                             "definition" (or "test" by path heuristic).
  textDocument/references  — returns 0..N locations, each a "callsite"
                             (or "test").

Anything resolving inside the anchor itself is skipped — those are the
already-visible lines, not "related" code.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from contracts.schemas import CodeAnchor, RelatedCode

from .detect import language_for_file
from .pool import LSPPool

log = logging.getLogger(__name__)


# Identifier patterns. We tolerate both snake_case and CamelCase; the
# stop-word lists below filter out the language-specific noise.
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

# Language-agnostic keywords / boring globals. Worth filtering here
# rather than per-language because the LSP would happily try to resolve
# `if` and waste a round-trip.
_STOP_WORDS = frozenset({
    # Python
    "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from",
    "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or",
    "pass", "raise", "return", "try", "while", "with", "yield",
    "True", "False", "None", "self", "cls",
    # JS/TS keywords (overlap with Python is fine)
    "const", "let", "var", "function", "export", "default", "interface",
    "type", "extends", "implements", "new", "this", "void", "null",
    "undefined", "true", "false", "async", "await",
    # Boring built-ins / common types
    "str", "int", "bool", "float", "list", "dict", "set", "tuple", "bytes",
    "object", "Any", "Optional", "Union", "Callable", "string", "number",
    "boolean", "Array", "Promise", "Map", "Set", "Record",
    "print", "len", "range", "isinstance", "issubclass",
    "console", "log",
})

# Patterns that mean "this is an import line — don't seed off it".
_IMPORT_LINE_RES = (
    re.compile(r"^\s*from\s+\S+\s+import\b"),          # py
    re.compile(r"^\s*import\s+\S+"),                    # py / js
    re.compile(r"^\s*import\s*\{.*\}\s*from\s+['\"]"),  # ts/js named import
    re.compile(r"^\s*import\s+\w+\s+from\s+['\"]"),     # ts/js default import
    re.compile(r"^\s*export\s*\{.*\}\s*from\s+['\"]"),  # ts/js re-export
    re.compile(r"\brequire\s*\(\s*['\"]"),              # cjs (const X = require("y"))
)


MAX_IDENTIFIERS = 8
MAX_RESULTS = 12


def _is_import_line(line: str) -> bool:
    return any(p.search(line) for p in _IMPORT_LINE_RES)


def _is_test_path(path: str) -> bool:
    s = path.replace("\\", "/")
    return (
        "/tests/" in s or s.startswith("tests/")
        or "__tests__/" in s
        or "/test/" in s
        or s.endswith("_test.py")
        or "/test_" in s
        or s.startswith("test_")
        or s.endswith(".spec.ts") or s.endswith(".spec.tsx")
        or s.endswith(".test.ts") or s.endswith(".test.tsx")
        or s.endswith(".spec.js") or s.endswith(".test.js")
    )


def _uri_to_path(uri: str) -> Path | None:
    """Decode a file:// URI back to a Path. Returns None for non-file URIs."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def _read_snippet_around(
    path: Path, line_1indexed: int, before: int = 1, after: int = 4,
) -> tuple[int, int, str]:
    """Read lines around `line_1indexed`. Returns (start, end, text)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return (line_1indexed, line_1indexed, "")
    start = max(1, line_1indexed - before)
    end = min(len(lines), line_1indexed + after)
    return start, end, "\n".join(lines[start - 1:end])


class LSPContextRetriever:
    """Find references / definitions via a language server."""

    def __init__(self, pool: LSPPool) -> None:
        self._pool = pool

    def is_available(self, file_path: str) -> bool:
        lang = language_for_file(file_path)
        return lang is not None and self._pool.is_available(lang)

    async def related(
        self,
        anchor: CodeAnchor,
        repo_root: Path,
        seed_lines: set[int] | None = None,
    ) -> list[RelatedCode]:
        """Look up related code via LSP.

        ``seed_lines`` (1-indexed, new-side) restricts identifier mining
        to those specific lines within the anchor range. Callers pass
        the set of ``+`` lines from the chunk's hunks so the retriever
        seeds only from lines that actually changed — context lines
        around the change are still "already visible" (filtered out of
        results) but don't contribute noise identifiers. When ``None``,
        the full anchor range is mined (legacy behaviour).
        """
        lang = language_for_file(anchor.file)
        if lang is None:
            return []
        client = await self._pool.get(lang, repo_root)
        if client is None:
            return []

        anchor_path = repo_root / anchor.file
        if not anchor_path.is_file():
            return []
        try:
            source = anchor_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        source_lines = source.splitlines()

        a_start, a_end = anchor.line_range
        anchor_uri = anchor_path.resolve().as_uri()

        # Open the file in the server before querying. Idempotent for
        # subsequent calls because did_open with the same URI just
        # refreshes the buffer on the server side.
        try:
            await client.did_open(anchor_uri, lang, source)
        except Exception as exc:
            log.info("LSP did_open failed: %s", exc)
            return []

        # Which lines to mine? If the caller pinned seed_lines to the
        # changed (+) lines, use those; otherwise scan the whole anchor.
        # An explicitly-empty seed_lines means "the chunk has nothing
        # LSP-queryable here" (e.g. deletion-only chunk) — return early.
        if seed_lines is not None:
            if not seed_lines:
                return []
            candidate_lines = sorted(n for n in seed_lines if a_start <= n <= a_end)
        else:
            candidate_lines = list(range(a_start, a_end + 1))

        # Collect identifiers from non-import candidate lines, preserving
        # source order so we explore the most "user-visible" symbols first.
        seeds: list[tuple[int, int, str]] = []  # (line, col, ident)
        seen_idents: set[str] = set()
        for line_no in candidate_lines:
            if not (1 <= line_no <= len(source_lines)):
                continue
            line_str = source_lines[line_no - 1]
            if _is_import_line(line_str):
                continue
            for m in _IDENT_RE.finditer(line_str):
                ident = m.group(0)
                if ident in _STOP_WORDS or ident in seen_idents:
                    continue
                if ident.startswith("__") and ident.endswith("__"):
                    continue
                seen_idents.add(ident)
                seeds.append((line_no, m.start(), ident))
                if len(seeds) >= MAX_IDENTIFIERS:
                    break
            if len(seeds) >= MAX_IDENTIFIERS:
                break

        if not seeds:
            return []

        repo_root_resolved = repo_root.resolve()
        results: list[RelatedCode] = []
        seen_keys: set[tuple[str, int]] = set()

        for line_no, col, ident in seeds:
            zero_line = line_no - 1
            # 1. Definition (one hit typically; LSP returns more for overloads)
            try:
                defs = await client.definition(anchor_uri, zero_line, col)
            except Exception as exc:
                log.debug("LSP definition failed for %s: %s", ident, exc)
                defs = []
            for d in defs[:1]:
                hit = _location_to_related(
                    d, repo_root_resolved, anchor.file, a_start, a_end, "definition",
                )
                if hit is None:
                    continue
                key = (hit.anchor.file, hit.anchor.line_range[0])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                results.append(hit)
                if len(results) >= MAX_RESULTS:
                    return results

            # 2. References
            try:
                refs = await client.references(anchor_uri, zero_line, col)
            except Exception as exc:
                log.debug("LSP references failed for %s: %s", ident, exc)
                refs = []
            for r in refs:
                hit = _location_to_related(
                    r, repo_root_resolved, anchor.file, a_start, a_end, "callsite",
                )
                if hit is None:
                    continue
                key = (hit.anchor.file, hit.anchor.line_range[0])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                results.append(hit)
                if len(results) >= MAX_RESULTS:
                    return results

        return results


def _location_to_related(
    loc: dict, repo_root: Path, anchor_file: str, a_start: int, a_end: int,
    fallback_kind: str,
) -> RelatedCode | None:
    """Convert an LSP Location to a RelatedCode entry. Returns None if the
    target is outside the repo or inside the anchor range itself."""
    uri = loc.get("uri")
    rng = loc.get("range") or {}
    start = rng.get("start") or {}
    line_0 = start.get("line")
    if uri is None or line_0 is None:
        return None
    abs_path = _uri_to_path(uri)
    if abs_path is None:
        return None
    try:
        rel = abs_path.relative_to(repo_root)
    except ValueError:
        return None
    rel_str = str(rel).replace("\\", "/")
    line_1 = int(line_0) + 1
    if rel_str == anchor_file and a_start <= line_1 <= a_end:
        return None  # this *is* the anchor line; not "related"
    snip_start, snip_end, snippet = _read_snippet_around(abs_path, line_1)
    relationship = "test" if _is_test_path(rel_str) else fallback_kind
    return RelatedCode(
        anchor=CodeAnchor(file=rel_str, line_range=(snip_start, snip_end)),
        relationship=relationship,
        snippet=snippet,
    )
