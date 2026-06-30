"""User + repo configuration files for the CLI entry point.

Two files, both TOML, both optional:

  ~/.config/pr-walkthrough/config.toml
    Global per-user defaults — applied to every run unless overridden by
    a repo-local file or CLI flag.

  <repo_root>/.pr-walkthrough/config.toml
    Per-repo overrides — primarily the resolved paths to language
    servers picked up in the LSP setup flow. Meant to be gitignored
    (the CLI writes a .gitignore alongside it on first write).

Reading is layered: defaults < global < repo. Writes always go to a
single file; callers pick the scope (`save_global_config`,
`save_repo_config`).

We keep the schema permissive — unknown keys are preserved on round-trip
so a newer CLI version can write fields an older version can read past
without losing data.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- paths


def global_config_dir() -> Path:
    """XDG-friendly path: $XDG_CONFIG_HOME/pr-walkthrough, falling back to ~/.config."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "pr-walkthrough"


def global_config_path() -> Path:
    return global_config_dir() / "config.toml"


def repo_config_dir(repo_root: Path) -> Path:
    return repo_root / ".pr-walkthrough"


def repo_config_path(repo_root: Path) -> Path:
    return repo_config_dir(repo_root) / "config.toml"


# --------------------------------------------------------------------------- schema


@dataclass
class CacheConfig:
    """Persistent narration + TTS cache. Opt-in so we don't fill other
    machines' disks by default; the CLI opts the local user in on first
    run."""

    enabled: bool = False
    max_gb: float = 1.0


@dataclass
class Config:
    """Merged config seen by the rest of the app. Fields here are the
    union of global + repo overrides; nothing here is repo-specific
    except `lsp_paths` (repo-resolved binaries)."""

    familiarity: str | None = None
    """Default familiarity if --familiarity is omitted and stdin isn't a
    TTY (interactive prompt otherwise). None == always prompt."""

    tts_engine: str = "say"
    """Default TTS engine. Overridable per-run via the variants menu."""

    cache: CacheConfig = field(default_factory=CacheConfig)

    lsp_paths: dict[str, str] = field(default_factory=dict)
    """language -> absolute path to language server binary. Populated by
    the LSP setup flow on first run for a repo."""


# --------------------------------------------------------------------------- io


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        # Treat unreadable / malformed files as absent rather than
        # crashing the CLI mid-run. The CLI surfaces a warning.
        return {}


def _merge(into: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge — override wins. Lists are replaced, not concat'd."""
    out = dict(into)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _from_dict(data: dict[str, Any]) -> Config:
    cache = data.get("cache") or {}
    return Config(
        familiarity=data.get("familiarity"),
        tts_engine=data.get("tts_engine", "say"),
        cache=CacheConfig(
            enabled=bool(cache.get("enabled", False)),
            max_gb=float(cache.get("max_gb", 1.0)),
        ),
        lsp_paths=dict(data.get("lsp_paths") or {}),
    )


def load_config(repo_root: Path | None = None) -> Config:
    """Load global + repo configs, merge, return the effective Config.

    Missing files are treated as empty. The repo file (if given) takes
    precedence over the global file.
    """
    merged: dict[str, Any] = {}
    merged = _merge(merged, _load_toml(global_config_path()))
    if repo_root is not None:
        merged = _merge(merged, _load_toml(repo_config_path(repo_root)))
    return _from_dict(merged)


def _serialise_toml(data: dict[str, Any]) -> str:
    """Hand-rolled TOML writer. Limited to the shapes our schema needs
    (top-level scalars, `[cache]` table, `[lsp_paths]` string-string
    table) so we don't pull a third-party dep just to write configs."""
    lines: list[str] = []
    for key in ("familiarity", "tts_engine"):
        if key in data and data[key] is not None:
            lines.append(f'{key} = "{data[key]}"')
    if data.get("cache"):
        lines.append("")
        lines.append("[cache]")
        for k, v in data["cache"].items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f'{k} = "{v}"')
    if data.get("lsp_paths"):
        lines.append("")
        lines.append("[lsp_paths]")
        for lang, path in data["lsp_paths"].items():
            lines.append(f'{lang} = "{path}"')
    return "\n".join(lines) + "\n"


def _config_to_dict(cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if cfg.familiarity:
        out["familiarity"] = cfg.familiarity
    if cfg.tts_engine and cfg.tts_engine != "say":
        out["tts_engine"] = cfg.tts_engine
    cache = asdict(cfg.cache)
    if cache != asdict(CacheConfig()):
        out["cache"] = cache
    if cfg.lsp_paths:
        out["lsp_paths"] = dict(cfg.lsp_paths)
    return out


def save_global_config(cfg: Config) -> Path:
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialise_toml(_config_to_dict(cfg)), encoding="utf-8")
    return path


def save_repo_config(repo_root: Path, cfg: Config) -> Path:
    """Write the repo-scoped config, dropping a .gitignore next to it on
    first write so the file doesn't end up in git history."""
    cfg_dir = repo_config_dir(repo_root)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    gi = cfg_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("# pr-walkthrough local config — do not commit\n*\n", encoding="utf-8")
    path = repo_config_path(repo_root)
    path.write_text(_serialise_toml(_config_to_dict(cfg)), encoding="utf-8")
    return path


def ensure_global_config_exists() -> Config:
    """First-run helper: if no global config exists, write one with cache
    enabled (the CLI is launched by the human user, so the local machine
    is the one we're opting in). Returns the freshly-loaded config either
    way."""
    if not global_config_path().is_file():
        save_global_config(Config(cache=CacheConfig(enabled=True, max_gb=1.0)))
    return load_config()
