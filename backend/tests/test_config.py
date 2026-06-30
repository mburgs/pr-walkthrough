"""Config loading + write round-trip."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pr_walkthrough.config import (
    CacheConfig,
    Config,
    load_config,
    save_global_config,
    save_repo_config,
    repo_config_path,
)


def test_load_returns_defaults_when_no_files(tmp_path: Path) -> None:
    """No config file anywhere — should return a Config with defaults,
    not raise."""
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        cfg = load_config()
    assert cfg.familiarity is None
    assert cfg.tts_engine == "say"
    assert cfg.cache.enabled is False
    assert cfg.lsp_paths == {}


def test_global_round_trip(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        cfg = Config(
            familiarity="tour",
            tts_engine="kokoro",
            cache=CacheConfig(enabled=True, max_gb=2.5),
        )
        save_global_config(cfg)
        loaded = load_config()
    assert loaded.familiarity == "tour"
    assert loaded.tts_engine == "kokoro"
    assert loaded.cache.enabled is True
    assert loaded.cache.max_gb == 2.5


def test_repo_overrides_global(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_dir = tmp_path / "config"
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(cfg_dir)}):
        save_global_config(Config(familiarity="review", tts_engine="say"))
        save_repo_config(
            repo,
            Config(
                familiarity="tutorial",
                tts_engine="say",
                lsp_paths={"python": "/usr/local/bin/pyright"},
            ),
        )
        loaded = load_config(repo_root=repo)
    assert loaded.familiarity == "tutorial"
    assert loaded.lsp_paths == {"python": "/usr/local/bin/pyright"}


def test_repo_config_gets_gitignore(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    save_repo_config(repo, Config(lsp_paths={"python": "/usr/bin/pyright"}))
    cfg_path = repo_config_path(repo)
    gitignore = cfg_path.parent / ".gitignore"
    assert gitignore.exists()
    assert "*" in gitignore.read_text()


def test_malformed_toml_treated_as_empty(tmp_path: Path) -> None:
    """A broken config file shouldn't crash the CLI mid-run."""
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        # Write garbage to the global config path manually.
        from pr_walkthrough.config import global_config_path
        path = global_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is = not toml [[ at all", encoding="utf-8")
        cfg = load_config()
    assert cfg.familiarity is None  # back to defaults
