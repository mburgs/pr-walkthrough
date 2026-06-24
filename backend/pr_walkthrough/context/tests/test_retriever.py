"""Tests for RipgrepContextRetriever.

Fixture layout (under tests/fixtures/sample_repo/):
  sample_repo/
    __init__.py
    store.py     — defines ItemStore, add_item, get_item, remove_item
    service.py   — callsites: add_item, get_item, remove_item
  tests/
    __init__.py
    test_store.py — test file using ItemStore / add_item

We query the anchor store.py:20-35 (the ItemStore class / add_item method)
and expect:
  - at least one 'definition' result  (the class itself, inside store.py sibling
    or the re-definition hit)
  - at least one 'callsite' (service.py)
  - at least one 'test' (tests/test_store.py)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Ensure contracts are importable from the worktree root
_WORKTREE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent.parent
# That's: test file → tests/ → context/ → pr_walkthrough/ → backend/ → (worktree)
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))

from contracts.schemas import CodeAnchor, RelatedCode  # type: ignore
from pr_walkthrough.context.retriever import (
    RipgrepContextRetriever,
    RipgrepNotFoundError,
    _extract_symbols,
    _classify_hit,
    _is_test_path,
    _is_definition,
)

SAMPLE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


class TestIsTestPath:
    def test_recognises_tests_dir(self) -> None:
        assert _is_test_path("tests/test_foo.py")

    def test_recognises_test_prefix(self) -> None:
        assert _is_test_path("pkg/test_bar.py")

    def test_recognises_spec_suffix(self) -> None:
        assert _is_test_path("src/app.spec.ts")

    def test_rejects_normal_file(self) -> None:
        assert not _is_test_path("src/store.py")

    def test_rejects_testdata_dir(self) -> None:
        # "testdata" is NOT a test file
        assert not _is_test_path("testdata/store.py")


class TestIsDefinition:
    def test_python_def(self) -> None:
        assert _is_definition("def add_item(self, item: Item) -> None:")

    def test_python_class(self) -> None:
        assert _is_definition("class ItemStore:")

    def test_typescript_function(self) -> None:
        assert _is_definition("export function fetchUser(id: string): User {")

    def test_go_func(self) -> None:
        assert _is_definition("func NewStore() *Store {")

    def test_plain_call_not_definition(self) -> None:
        assert not _is_definition("    store.add_item(item)")


class TestExtractSymbols:
    def test_returns_top_tokens(self) -> None:
        lines = ["    def add_item(self, item: Item) -> None:", "        self._items[item.item_id] = item"]
        syms = _extract_symbols(lines)
        assert "add_item" in syms or "Item" in syms or "item_id" in syms

    def test_filters_stopwords(self) -> None:
        lines = ["    def foo(self, item):"]
        syms = _extract_symbols(lines)
        assert "self" not in syms
        assert "def" not in syms


class TestClassifyHit:
    def test_test_path_wins(self) -> None:
        rel = _classify_hit(
            hit_file="tests/test_store.py",
            hit_line="    store.add_item(item)",
            anchor_file="sample_repo/store.py",
            anchor_start=20,
            anchor_end=30,
            hit_lineno=15,
        )
        assert rel == "test"

    def test_sibling_same_file_outside_range(self) -> None:
        rel = _classify_hit(
            hit_file="sample_repo/store.py",
            hit_line="class ItemStore:",
            anchor_file="sample_repo/store.py",
            anchor_start=20,
            anchor_end=30,
            hit_lineno=5,
        )
        assert rel == "sibling"

    def test_skip_within_anchor_range(self) -> None:
        rel = _classify_hit(
            hit_file="sample_repo/store.py",
            hit_line="    def add_item(self):",
            anchor_file="sample_repo/store.py",
            anchor_start=20,
            anchor_end=30,
            hit_lineno=25,
        )
        assert rel == "__skip__"

    def test_definition_in_other_file(self) -> None:
        rel = _classify_hit(
            hit_file="sample_repo/other.py",
            hit_line="def add_item(store, item):",
            anchor_file="sample_repo/store.py",
            anchor_start=20,
            anchor_end=30,
            hit_lineno=5,
        )
        assert rel == "definition"

    def test_callsite_in_other_file(self) -> None:
        rel = _classify_hit(
            hit_file="sample_repo/service.py",
            hit_line="    _global_store.add_item(item)",
            anchor_file="sample_repo/store.py",
            anchor_start=20,
            anchor_end=30,
            hit_lineno=14,
        )
        assert rel == "callsite"


# ---------------------------------------------------------------------------
# Integration tests — requires ripgrep on PATH and the sample_repo fixture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_related_returns_definition_callsite_test() -> None:
    """Query add_item anchor; expect definition, callsite, and test results."""
    retriever = RipgrepContextRetriever()
    anchor = CodeAnchor(file="sample_repo/store.py", line_range=(24, 26))
    results = await retriever.related(anchor, SAMPLE_REPO)

    assert isinstance(results, list)
    assert len(results) > 0

    relationships = {r.relationship for r in results}
    # We must get at least a callsite (service.py) and a test (tests/test_store.py)
    assert "callsite" in relationships, f"Expected callsite, got: {relationships}"
    assert "test" in relationships, f"Expected test, got: {relationships}"

    # All results must be valid RelatedCode objects
    for r in results:
        assert isinstance(r, RelatedCode)
        assert r.snippet  # non-empty snippet
        assert r.anchor.file
        assert r.anchor.line_range[0] <= r.anchor.line_range[1]


@pytest.mark.asyncio
async def test_related_caps_at_max_results() -> None:
    """Results must never exceed MAX_RESULTS (8)."""
    from pr_walkthrough.context.retriever import MAX_RESULTS

    retriever = RipgrepContextRetriever()
    anchor = CodeAnchor(file="sample_repo/store.py", line_range=(1, 40))
    results = await retriever.related(anchor, SAMPLE_REPO)
    assert len(results) <= MAX_RESULTS


@pytest.mark.asyncio
async def test_related_ranking_definitions_first() -> None:
    """Definitions should appear before callsites in the result list."""
    retriever = RipgrepContextRetriever()
    anchor = CodeAnchor(file="sample_repo/store.py", line_range=(24, 26))
    results = await retriever.related(anchor, SAMPLE_REPO)

    from pr_walkthrough.context.retriever import _RANK
    prev_rank = -1
    for r in results:
        rank = _RANK.get(r.relationship, 99)
        assert rank >= prev_rank, (
            f"Result out of order: {r.relationship} after rank {prev_rank}"
        )
        prev_rank = rank


@pytest.mark.asyncio
async def test_missing_anchor_file_returns_empty() -> None:
    """If the anchor file does not exist, return an empty list gracefully."""
    retriever = RipgrepContextRetriever()
    anchor = CodeAnchor(file="nonexistent/ghost.py", line_range=(1, 5))
    results = await retriever.related(anchor, SAMPLE_REPO)
    assert results == []


@pytest.mark.asyncio
async def test_ripgrep_missing_raises_clear_error() -> None:
    """If rg is not on PATH, raise RipgrepNotFoundError with a helpful message."""
    retriever = RipgrepContextRetriever()
    anchor = CodeAnchor(file="sample_repo/store.py", line_range=(24, 26))

    # Patch asyncio.create_subprocess_exec to simulate missing rg
    with patch(
        "pr_walkthrough.context.retriever.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("rg not found"),
    ):
        with pytest.raises(RipgrepNotFoundError) as exc_info:
            await retriever.related(anchor, SAMPLE_REPO)

    assert "ripgrep" in str(exc_info.value).lower() or "rg" in str(exc_info.value)
