"""`pr-context` CLI: anchor parsing + the async main() error/success paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.schemas import CodeAnchor, RelatedCode
from pr_walkthrough.context.cli import _main, _parse_anchor, main
from pr_walkthrough.context.retriever import (
    RipgrepContextRetriever,
    RipgrepNotFoundError,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("store.py:10-15", ("store.py", 10, 15)),
        ("src/app/main.py:1-1", ("src/app/main.py", 1, 1)),
        # Greedy `.+` means a colon inside the path is handled correctly —
        # only the trailing `:N-N` is treated as the range.
        ("C:/repo/file.py:5-8", ("C:/repo/file.py", 5, 8)),
    ],
)
def test_parse_anchor_accepts_valid_forms(raw: str, expected: tuple[str, int, int]) -> None:
    assert _parse_anchor(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "store.py",           # no range at all
        "store.py:10",        # missing end
        "store.py:abc-def",   # non-numeric
        "",
    ],
)
def test_parse_anchor_rejects_bad_format(raw: str) -> None:
    with pytest.raises(ValueError, match="Invalid anchor format"):
        _parse_anchor(raw)


def test_parse_anchor_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="must be <="):
        _parse_anchor("store.py:15-10")


async def test_main_errors_when_repo_root_missing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    missing = tmp_path / "does-not-exist"
    code = await _main(str(missing), "store.py:1-2")
    assert code == 1
    assert "not a directory" in capsys.readouterr().err


async def test_main_errors_on_bad_anchor(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = await _main(str(tmp_path), "store.py")
    assert code == 1
    assert "Invalid anchor format" in capsys.readouterr().err


async def test_main_errors_when_ripgrep_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    async def _raise_not_found(self, anchor, repo_root, seed_lines=None):
        raise RipgrepNotFoundError("rg not found on PATH")

    monkeypatch.setattr(RipgrepContextRetriever, "related", _raise_not_found)
    code = await _main(str(tmp_path), "store.py:1-2")
    assert code == 1
    assert "rg not found" in capsys.readouterr().err


async def test_main_prints_json_results_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    result = RelatedCode(
        anchor=CodeAnchor(file="store.py", line_range=(3, 3)),
        relationship="definition",
        snippet="def store():\n    ...",
        target_line=3,
    )

    async def _fake_related(self, anchor, repo_root, seed_lines=None):
        return [result]

    monkeypatch.setattr(RipgrepContextRetriever, "related", _fake_related)
    code = await _main(str(tmp_path), "store.py:3-3")
    assert code == 0
    out = capsys.readouterr().out
    assert '"file": "store.py"' in out
    assert '"relationship": "definition"' in out


def test_main_sync_wrapper_exits_1_on_wrong_arg_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr("sys.argv", ["pr-context", "only-one-arg"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "Usage:" in capsys.readouterr().err
