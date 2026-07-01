"""Language detection from file paths + LSP binary discovery.

Two lookups, kept separate so the CLI install flow can stage them:

  language_for_file(path)         -> "python" | "typescript" | ... | None
  resolve_server_command(lang, …) -> [argv] or None  (None == not installed)

Server binaries we know how to drive:

  python      pyright-langserver --stdio        (preferred; pip install pyright)
  python      pylsp                             (fallback; pip install python-lsp-server)
  typescript  typescript-language-server --stdio
  javascript  typescript-language-server --stdio  (same server handles both)

Discovery order for each language:
  1. Path in the resolved Config.lsp_paths (set by the CLI install flow)
  2. Bare binary name on PATH

We do *not* try to install missing servers here — that's
`pr-walkthrough setup`'s job (see `pr_walkthrough.setup_cmd`). The
launcher (`cli_app`) only warns when a server is missing; detection
here stays read-only.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# File extension -> language id. Multiple extensions can map to one
# language; that's why this is a flat dict rather than a per-language
# extension list.
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}

# Language id -> ordered list of candidate (binary_name, extra_args).
# Tried in order; first one found on PATH wins. The argv tail is
# whatever the server needs to speak stdio (most need an explicit flag).
_LANG_SERVERS: dict[str, list[tuple[str, list[str]]]] = {
    "python": [
        ("pyright-langserver", ["--stdio"]),
        ("pylsp", []),
    ],
    "typescript": [
        ("typescript-language-server", ["--stdio"]),
    ],
    "javascript": [
        ("typescript-language-server", ["--stdio"]),
    ],
}


def language_for_file(path: str | Path) -> str | None:
    """Return the LSP language id for a file, or None if we don't drive
    a server for that extension."""
    suffix = Path(path).suffix.lower()
    return _EXT_TO_LANG.get(suffix)


def language_for_files(paths: list[str]) -> set[str]:
    """Set of languages that appear in `paths`. Drops Nones."""
    return {lang for p in paths if (lang := language_for_file(p)) is not None}


def resolve_server_command(
    language: str,
    configured_path: str | None = None,
) -> list[str] | None:
    """Return the argv that spawns the language server, or None if no
    server for `language` is installed.

    `configured_path` overrides discovery — if it points to an existing
    executable, it's used verbatim with the language's default flags.
    """
    candidates = _LANG_SERVERS.get(language, [])
    # 1. Configured override
    if configured_path:
        p = Path(configured_path).expanduser()
        if p.is_file():
            # Re-find the matching server entry to grab its flags.
            for name, args in candidates:
                if p.name == name or p.name.startswith(name):
                    return [str(p), *args]
            # Unknown server name but path exists — assume --stdio works.
            return [str(p), "--stdio"]
    # 2. PATH lookup
    for name, args in candidates:
        found = shutil.which(name)
        if found:
            return [found, *args]
    return None


def install_hint(language: str) -> str:
    """Human-readable install command for the canonical server."""
    if language == "python":
        return "pip install pyright"
    if language in ("typescript", "javascript"):
        return "npm install -g typescript-language-server typescript"
    return ""
