"""Jedi-backed ContextRetriever for Python files.

For a chunk anchor inside a `.py` file, we use Jedi to:

  - Find each identifier appearing on the anchor lines
  - Resolve its definition (`Script.goto`) — that's the "definition" relationship
  - Resolve its references (`Script.get_references`) — those are "callsites"
  - Filter out anything that lives INSIDE the anchor range itself

For non-Python files (or when Jedi can't be initialised), we fall back to the
existing ripgrep retriever via composition — see `HybridContextRetriever`
below.

Jedi is purely static — no LSP server to spawn, no language-server protocol.
For Python repos that's adequate; it handles import resolution, class/method
lookups, etc. without the operational overhead of pyright/pylsp.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from contracts.schemas import CodeAnchor, RelatedCode

logger = logging.getLogger(__name__)


# Patterns kept tight on purpose — we want symbol identifiers, not keywords.
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_PY_KEYWORDS = frozenset({
    "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
    "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass",
    "raise", "return", "try", "while", "with", "yield",
    "True", "False", "None", "self", "cls",
})
# Things that are technically identifiers but rarely useful to chase
_BORING = frozenset({
    "str", "int", "bool", "float", "list", "dict", "set", "tuple", "bytes",
    "None", "object", "type", "Any", "Optional", "Union", "Callable",
    "print", "len", "range", "isinstance", "issubclass",
})


def _interesting_identifiers(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in _IDENT_RE.finditer(text):
        s = match.group(0)
        if s in _PY_KEYWORDS or s in _BORING:
            continue
        if s.startswith("__") and s.endswith("__"):
            continue  # dunder methods — too noisy
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _read_snippet(path: Path, line: int, before: int = 1, after: int = 4) -> str:
    """Return ~5 lines around `line` from `path` (1-indexed)."""
    try:
        all_lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    start = max(0, line - 1 - before)
    end = min(len(all_lines), line - 1 + after + 1)
    return "\n".join(all_lines[start:end])


def _is_test_path(p: Path) -> bool:
    s = str(p).replace("\\", "/")
    return (
        "/tests/" in s
        or s.startswith("tests/")
        or "__tests__/" in s
        or "/test/" in s
        or s.endswith("_test.py")
        or "/test_" in s
        or s.startswith("test_")
    )


def _identifier_at(line: str, col: int) -> tuple[int, int] | None:
    """Find the identifier overlapping column `col` in `line`. Returns (start, end_col)."""
    for m in _IDENT_RE.finditer(line):
        if m.start() <= col < m.end():
            return m.start(), m.end()
    return None


class JediContextRetriever:
    """Resolves cross-repo references for Python anchors via Jedi.

    Initialised with no state; each `related()` call is independent.
    """

    @classmethod
    def is_available(cls) -> bool:
        try:
            import jedi  # noqa: F401
            return True
        except ImportError:
            return False

    async def related(
        self, anchor: CodeAnchor, repo_root: Path,
    ) -> list[RelatedCode]:
        if not anchor.file.endswith(".py"):
            return []
        anchor_path = repo_root / anchor.file
        if not anchor_path.exists() or not anchor_path.is_file():
            return []
        return await asyncio.to_thread(self._related_sync, anchor, repo_root)

    def _related_sync(self, anchor: CodeAnchor, repo_root: Path) -> list[RelatedCode]:
        try:
            import jedi
        except ImportError:
            return []

        anchor_path = repo_root / anchor.file
        try:
            source = anchor_path.read_text(errors="replace")
        except OSError:
            return []

        source_lines = source.splitlines()
        a_start, a_end = anchor.line_range
        # Lines in the anchor range we'll mine for identifiers
        anchor_text = "\n".join(
            source_lines[max(0, line - 1)]
            for line in range(a_start, a_end + 1)
            if 1 <= line <= len(source_lines)
        )
        if not anchor_text.strip():
            return []

        # Jedi needs a Project rooted at the repo so it can resolve imports.
        try:
            project = jedi.Project(path=str(repo_root))
        except Exception as exc:
            logger.info("Jedi project setup failed for %s: %s", repo_root, exc)
            return []

        try:
            script = jedi.Script(code=source, path=str(anchor_path), project=project)
        except Exception as exc:
            logger.info("Jedi Script init failed for %s: %s", anchor_path, exc)
            return []

        results: list[RelatedCode] = []
        seen_keys: set[tuple[str, int]] = set()

        # Walk identifiers in each anchor line. For each, ask Jedi for its
        # definition and references. Cap the work so a huge anchor doesn't
        # explode the token budget downstream.
        MAX_ANCHOR_IDENTIFIERS = 8
        MAX_RESULTS = 12

        anchor_files = {anchor.file}
        identifiers_visited: set[str] = set()

        for line_idx in range(a_start, a_end + 1):
            if 1 <= line_idx <= len(source_lines):
                line_str = source_lines[line_idx - 1]
            else:
                continue

            for ident in _interesting_identifiers(line_str):
                if ident in identifiers_visited:
                    continue
                identifiers_visited.add(ident)
                if len(identifiers_visited) > MAX_ANCHOR_IDENTIFIERS:
                    break

                # Find the column where the identifier appears, ask Jedi there
                col_match = re.search(re.escape(ident), line_str)
                if not col_match:
                    continue
                col = col_match.start() + 1  # mid-identifier; Jedi accepts any col within

                try:
                    defs = script.goto(line=line_idx, column=col, follow_imports=True)
                except Exception:
                    defs = []

                # Definitions — single hit per symbol, in repo only
                for d in defs[:1]:
                    if not getattr(d, "module_path", None):
                        continue
                    try:
                        rel = Path(d.module_path).relative_to(repo_root)
                    except ValueError:
                        continue  # outside repo (stdlib, deps)
                    rel_str = str(rel)
                    if rel_str in anchor_files and d.line and a_start <= d.line <= a_end:
                        continue  # the definition is INSIDE the anchor itself — skip
                    key = (rel_str, d.line or 0)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    snippet = _read_snippet(Path(d.module_path), d.line or 1)
                    relationship = "test" if _is_test_path(rel) else "definition"
                    results.append(RelatedCode(
                        anchor=CodeAnchor(
                            file=rel_str,
                            line_range=(d.line or 1, (d.line or 1) + snippet.count("\n")),
                        ),
                        relationship=relationship,
                        snippet=snippet,
                    ))
                    if len(results) >= MAX_RESULTS:
                        return results

                # References — Jedi finds callsites too
                try:
                    refs = script.get_references(line=line_idx, column=col, include_builtins=False)
                except Exception:
                    refs = []
                for r in refs:
                    if not getattr(r, "module_path", None):
                        continue
                    try:
                        rel = Path(r.module_path).relative_to(repo_root)
                    except ValueError:
                        continue
                    rel_str = str(rel)
                    line = r.line or 0
                    if rel_str in anchor_files and a_start <= line <= a_end:
                        continue  # the reference IS the anchor — skip
                    key = (rel_str, line)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    snippet = _read_snippet(Path(r.module_path), line)
                    relationship = "test" if _is_test_path(rel) else "callsite"
                    results.append(RelatedCode(
                        anchor=CodeAnchor(
                            file=rel_str,
                            line_range=(line, line + snippet.count("\n")),
                        ),
                        relationship=relationship,
                        snippet=snippet,
                    ))
                    if len(results) >= MAX_RESULTS:
                        return results

            if len(identifiers_visited) > MAX_ANCHOR_IDENTIFIERS:
                break

        return results


class HybridContextRetriever:
    """Jedi for .py files; ripgrep for everything else."""

    def __init__(self) -> None:
        self._jedi = JediContextRetriever() if JediContextRetriever.is_available() else None
        # Lazy import — ripgrep retriever doesn't run unless we hit a non-Python file
        from .retriever import RipgrepContextRetriever
        self._rg = RipgrepContextRetriever()

    async def related(
        self, anchor: CodeAnchor, repo_root: Path,
    ) -> list[RelatedCode]:
        if self._jedi is not None and anchor.file.endswith(".py"):
            try:
                hits = await self._jedi.related(anchor, repo_root)
                if hits:
                    return hits
            except Exception:
                logger.warning("Jedi retriever failed; falling back to ripgrep", exc_info=True)
        return await self._rg.related(anchor, repo_root)
