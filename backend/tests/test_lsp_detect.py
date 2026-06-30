"""Language detection + LSP binary discovery."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pr_walkthrough.context.lsp.detect import (
    install_hint,
    language_for_file,
    language_for_files,
    resolve_server_command,
)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/auth.py", "python"),
        ("types.pyi", "python"),
        ("frontend/App.tsx", "typescript"),
        ("frontend/index.ts", "typescript"),
        ("util.js", "javascript"),
        ("server.mjs", "javascript"),
        ("README.md", None),
        ("Cargo.toml", None),
        ("Dockerfile", None),
    ],
)
def test_language_for_file(path: str, expected: str | None) -> None:
    assert language_for_file(path) == expected


def test_language_for_files_dedupes() -> None:
    files = ["a.py", "b.py", "c.tsx", "d.ts", "Makefile"]
    assert language_for_files(files) == {"python", "typescript"}


def test_resolve_uses_configured_path(tmp_path: Path) -> None:
    fake = tmp_path / "pyright-langserver"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    cmd = resolve_server_command("python", configured_path=str(fake))
    assert cmd is not None
    assert cmd[0] == str(fake)
    assert "--stdio" in cmd


def test_resolve_falls_back_to_path() -> None:
    with patch("pr_walkthrough.context.lsp.detect.shutil.which") as which:
        which.side_effect = lambda name: f"/usr/local/bin/{name}" if name == "pyright-langserver" else None
        cmd = resolve_server_command("python")
        assert cmd == ["/usr/local/bin/pyright-langserver", "--stdio"]


def test_resolve_returns_none_when_unavailable() -> None:
    with patch("pr_walkthrough.context.lsp.detect.shutil.which", return_value=None):
        assert resolve_server_command("python") is None
        assert resolve_server_command("typescript") is None


def test_install_hint_for_known_languages() -> None:
    assert "pyright" in install_hint("python")
    assert "typescript-language-server" in install_hint("typescript")
    assert install_hint("rust") == ""  # unknown language
