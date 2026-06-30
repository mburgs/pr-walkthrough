"""LSP retriever — unit tests with a fake LSPClient.

We don't actually spawn pyright here; the retriever's logic (skipping
imports, classifying hits, dropping anchor-internal matches) is tested
against a hand-driven fake client. Integration with a real server is
covered by hand-running the CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from contracts.schemas import CodeAnchor
from pr_walkthrough.context.lsp.retriever import (
    LSPContextRetriever,
    _is_import_line,
    _is_test_path,
)


class _FakePool:
    def __init__(self, lookups: dict[str, Any]) -> None:
        self._client = _FakeClient(lookups)

    def is_available(self, language: str) -> bool:
        return True

    async def get(self, language: str, repo_root: Path) -> "_FakeClient":
        return self._client


class _FakeClient:
    """Minimal in-memory LSP client: returns canned locations for
    pre-recorded identifiers."""

    def __init__(self, lookups: dict[str, Any]) -> None:
        # lookups maps identifier -> {"definition": [Locations], "references": [Locations]}
        self._lookups = lookups
        self.opened: list[str] = []

    async def did_open(self, uri: str, lang: str, text: str) -> None:
        self.opened.append(uri)

    async def definition(self, uri: str, line: int, col: int) -> list[dict]:
        return self._latest_ident_lookup("definition", uri, line, col)

    async def references(self, uri: str, line: int, col: int) -> list[dict]:
        return self._latest_ident_lookup("references", uri, line, col)

    def _latest_ident_lookup(self, kind: str, uri: str, line: int, col: int) -> list[dict]:
        # Resolve the ident at (line, col) from the actual file on disk
        from urllib.parse import unquote, urlparse
        path = Path(unquote(urlparse(uri).path))
        src = path.read_text().splitlines()
        line_text = src[line] if 0 <= line < len(src) else ""
        # Find identifier at the column
        import re
        for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", line_text):
            if m.start() <= col < m.end():
                return self._lookups.get(m.group(0), {}).get(kind, [])
        return []


@pytest.mark.parametrize(
    "line,expected",
    [
        ("from core.models import Calendar, Transparency", True),
        ("import os", True),
        ("import { foo } from 'bar'", True),
        ("import bar from 'bar'", True),
        ('const mod = require("x");', True),
        ("    foo = Transparency.OPAQUE", False),
        ("def make() -> None:", False),
        ("    return x.value", False),
    ],
)
def test_is_import_line(line: str, expected: bool) -> None:
    assert _is_import_line(line) is expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("tests/test_x.py", True),
        ("src/auth/__tests__/auth.test.tsx", True),
        ("src/util.test.ts", True),
        ("src/util.spec.ts", True),
        ("src/util.py", False),
        ("test_something.py", True),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert _is_test_path(path) is expected


async def test_skips_import_lines(tmp_path: Path) -> None:
    """The hard requirement from the related-code regression: when an
    anchor's only mention of a symbol is on an import line, that symbol
    should not seed any LSP queries.

    Setup: a 3-line anchor whose first line is an import. The fake
    client *would* return references for the imported identifier if
    asked; the retriever must not ask."""
    src = tmp_path / "src.py"
    src.write_text(
        "from core.models import Transparency\n"
        "def make():\n"
        "    return organizer_email\n"
    )
    # Set up the fake to *yell* if Transparency is queried — if the
    # retriever asks, we'd see a transparency-flavoured hit. organizer_email
    # has a real callsite.
    lookups = {
        "Transparency": {
            "definition": [{"uri": (tmp_path / "core" / "models.py").as_uri(), "range": {"start": {"line": 0}}}],
            "references": [{"uri": (tmp_path / "wrong.py").as_uri(), "range": {"start": {"line": 0}}}],
        },
        "organizer_email": {
            "definition": [{"uri": (tmp_path / "schema.py").as_uri(), "range": {"start": {"line": 4}}}],
            "references": [],
        },
    }
    # Create the target files so the snippet reader finds something
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "models.py").write_text("class Transparency: ...\n")
    (tmp_path / "wrong.py").write_text("Transparency.OPAQUE\n")
    (tmp_path / "schema.py").write_text("\n\n\n\norganizer_email: str\n")

    retriever = LSPContextRetriever(_FakePool(lookups))
    anchor = CodeAnchor(file="src.py", line_range=(1, 3))
    hits = await retriever.related(anchor, tmp_path)

    files = {h.anchor.file for h in hits}
    assert "wrong.py" not in files, "Transparency import-line seeded a query"
    assert "core/models.py" not in files
    # organizer_email's def should be picked up though
    assert "schema.py" in files


async def test_drops_anchor_internal_hits(tmp_path: Path) -> None:
    """A reference pointing back into the anchor range itself is not
    'related' code — it's the anchor."""
    src = tmp_path / "src.py"
    src.write_text(
        "def helper():\n"
        "    return helper()\n"
    )
    lookups = {
        "helper": {
            "definition": [],
            # Both refs are inside the anchor range
            "references": [
                {"uri": src.as_uri(), "range": {"start": {"line": 0}}},
                {"uri": src.as_uri(), "range": {"start": {"line": 1}}},
            ],
        },
    }
    retriever = LSPContextRetriever(_FakePool(lookups))
    anchor = CodeAnchor(file="src.py", line_range=(1, 2))
    hits = await retriever.related(anchor, tmp_path)
    assert hits == []


async def test_classifies_test_path(tmp_path: Path) -> None:
    src = tmp_path / "src.py"
    src.write_text("def widget(): pass\n")
    test_file = tmp_path / "tests" / "test_widget.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_widget():\n    assert widget() is None\n")
    lookups = {
        "widget": {
            "definition": [],
            "references": [
                {"uri": test_file.as_uri(), "range": {"start": {"line": 1}}},
            ],
        },
    }
    retriever = LSPContextRetriever(_FakePool(lookups))
    anchor = CodeAnchor(file="src.py", line_range=(1, 1))
    hits = await retriever.related(anchor, tmp_path)
    assert len(hits) == 1
    assert hits[0].relationship == "test"
