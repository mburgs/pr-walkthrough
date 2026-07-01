"""`pr-walkthrough setup` — optional post-install configuration.

Installing the package (via scripts/install.sh or the manual pip/npm
steps in the README) gets you a *runnable* CLI. This command gets you a
*nice* one: it offers to install a better TTS voice, warms up the local
STT model, installs language servers for the languages you pick, and
writes your config file.

Design constraints:

  * Idempotent. Re-running only touches what's missing — anything
    already installed/configured is reported and skipped. Safe to run
    after every `git pull`.
  * No raw subprocess noise. Successful installs print one ✓ line;
    failures print a short diagnosis (the tail of stderr), never a raw
    traceback, so a first-time user isn't staring at pip's output.
  * Nothing here is required to run `pr-walkthrough <pr>` — every
    feature it configures has a graceful fallback (ripgrep for LSP,
    macOS `say` for TTS, a warning for STT). This command exists to
    upgrade those fallbacks, not to gate the app on them.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from pr_walkthrough import config as cfg_mod
from pr_walkthrough.cli_style import S, err, ok, print_failure_detail, section, step, warn
from pr_walkthrough.venv_util import find_venv_bin, project_dirs

_FAMILIARITY_LEVELS = ("tutorial", "tour", "review", "highlights")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pr-walkthrough setup",
        description=(
            "Configure optional features: TTS/STT models, language "
            "servers, and your config file. Safe to re-run."
        ),
    )
    p.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Accept recommended defaults without prompting (non-interactive).",
    )
    return p.parse_args(argv)


# --------------------------------------------------------------------------- prompts


def _confirm(args: argparse.Namespace, prompt: str, default: bool) -> bool:
    if args.yes or not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    ans = input(f"  ? {prompt} {suffix} ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _choose(args: argparse.Namespace, prompt: str, options: tuple[str, ...], default: str) -> str:
    if args.yes or not sys.stdin.isatty():
        return default
    labels = "/".join(o if o != default else o.upper() for o in options)
    ans = input(f"  ? {prompt} [{labels}] ").strip().lower()
    if not ans:
        return default
    return ans if ans in options else default


# --------------------------------------------------------------------------- guard


def _guard(label: str, fn):
    """Run a setup phase; catch anything unexpected so one broken step
    doesn't abort the rest of setup. Returns (ok, result); result is
    None on failure."""
    try:
        return True, fn()
    except Exception as e:
        err(f"{label}: unexpected error — {e}")
        if os.environ.get("PR_WALKTHROUGH_DEBUG"):
            import traceback
            traceback.print_exc()
        else:
            print(S.dim("    (set PR_WALKTHROUGH_DEBUG=1 to see the full traceback)"),
                  file=sys.stderr, flush=True)
        return False, None


def _run_install(cmd: list[str], *, timeout: float = 300.0) -> bool:
    """Run an install command quietly; only surface output on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        err(f"`{cmd[0]}` isn't on PATH")
        return False
    except subprocess.TimeoutExpired:
        err(f"`{' '.join(cmd)}` timed out")
        return False
    if result.returncode != 0:
        err(f"`{' '.join(cmd)}` failed")
        print_failure_detail(result.stderr or result.stdout)
        return False
    return True


# --------------------------------------------------------------------------- dependency check


def _check_ripgrep() -> None:
    section("Dependencies")
    if shutil.which("rg"):
        ok("ripgrep found")
        return
    warn("ripgrep ('rg') not found — related-code search will fail without it")
    print(S.dim("    macOS:            brew install ripgrep"))
    print(S.dim("    Debian/Ubuntu:    sudo apt-get install ripgrep"))
    print(S.dim("    other:            https://github.com/BurntSushi/ripgrep#installation"))


# --------------------------------------------------------------------------- TTS


_TTS_CHOICES = (
    ("kokoro", "kokoro", "recommended — high quality, ~300 MB download"),
    ("piper", "piper", "fast, smaller download"),
    ("say", None, "macOS built-in — no install needed" if platform.system() == "Darwin" else "macOS only"),
)


def _setup_tts(args: argparse.Namespace, backend_dir: Path) -> str | None:
    section("Text-to-speech")
    from pr_walkthrough.tts.kokoro_adapter import KokoroTTSAdapter
    from pr_walkthrough.tts.piper_adapter import PiperTTSAdapter
    from pr_walkthrough.tts.say_adapter import SayTTSAdapter

    if KokoroTTSAdapter.is_available():
        ok("kokoro already installed (best quality)")
        return "kokoro"
    if PiperTTSAdapter.is_available():
        ok("piper already installed")
        return "piper"
    if SayTTSAdapter.is_available():
        print(S.dim("    macOS `say` works today but sounds robotic."))

    print("  Pick a voice engine to install:")
    for name, _extra, blurb in _TTS_CHOICES:
        print(f"    {name:<8} {S.dim('— ' + blurb)}")
    choice = _choose(args, "Install which engine?", ("kokoro", "piper", "say"), default="kokoro")

    extra = dict((n, e) for n, e, _ in _TTS_CHOICES).get(choice)
    if extra is None:
        if choice == "say" and not SayTTSAdapter.is_available():
            warn("macOS `say` isn't available on this platform — skipping TTS install")
            return None
        ok(f"using {choice} — nothing to install")
        return choice

    project_root = backend_dir.parent
    pip_exe = find_venv_bin(project_root, "pip")
    step(f"installing {choice} (this can take a minute)…")
    if _run_install([pip_exe, "install", "-e", f"{backend_dir}[{extra}]"]):
        ok(f"{choice} installed")
        return choice
    warn(f"{choice} install failed — falling back to macOS `say` / whatever's available")
    return None


# --------------------------------------------------------------------------- STT


def _setup_stt(args: argparse.Namespace) -> None:
    section("Speech-to-text (voice follow-ups)")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        warn("voice follow-ups require an Apple Silicon Mac — skipping")
        return
    try:
        import parakeet_mlx  # noqa: F401
    except ImportError:
        err("parakeet-mlx isn't installed — voice follow-ups won't work")
        print(S.dim("    reinstall: pip install -e backend"))
        return
    ok("parakeet-mlx installed")
    if not _confirm(args, "Download the speech-to-text model now (~600 MB, otherwise happens on first use)?", default=True):
        return
    step("downloading speech-to-text model…")
    from pr_walkthrough.stt.parakeet_adapter import ParakeetSTTAdapter
    ParakeetSTTAdapter().warmup()
    ok("speech-to-text model ready")


# --------------------------------------------------------------------------- LSP


_LSP_LANGS: tuple[str, ...] = ("python", "typescript")


def _setup_lsp(args: argparse.Namespace) -> None:
    section('Language servers (precise "find references" during walkthroughs)')
    print(S.dim("    Falls back to ripgrep search automatically for any language you skip.\n"))
    from pr_walkthrough.context.lsp.detect import install_hint, resolve_server_command

    for lang in _LSP_LANGS:
        if resolve_server_command(lang):
            ok(f"{lang}: already available")
            continue
        hint = install_hint(lang)
        if not _confirm(args, f"Install LSP for {lang}? (`{hint}`)", default=True):
            warn(f"{lang}: skipped — install later with `{hint}`")
            continue
        step(f"installing {lang} LSP…")
        if _run_install(hint.split()) and resolve_server_command(lang):
            ok(f"{lang} LSP installed")
        else:
            warn(f"{lang} LSP install failed — ripgrep fallback will be used")


# --------------------------------------------------------------------------- config


def _setup_config(args: argparse.Namespace, tts_engine: str | None) -> None:
    section("Config file")
    path = cfg_mod.global_config_path()
    existed = path.is_file()
    cfg = cfg_mod.load_config()

    if not cfg.cache.enabled:
        cfg.cache.enabled = True

    if tts_engine:
        cfg.tts_engine = tts_engine

    if _confirm(args, "Set a default familiarity level so you're not asked every run?",
                default=cfg.familiarity is not None):
        level = _choose(args, "Default familiarity", _FAMILIARITY_LEVELS,
                         default=cfg.familiarity or "review")
        cfg.familiarity = level

    cfg_mod.save_global_config(cfg)
    if existed:
        ok(f"config updated at {path}")
    else:
        ok(f"config written to {path}")


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    section("pr-walkthrough setup")
    print("  Configures optional voice models, language servers, and your config file.")
    print(S.dim("  Safe to re-run any time — already-installed pieces are skipped.\n"), flush=True)

    _, backend_dir, _ = project_dirs(Path(__file__))

    all_ok = True
    step_ok, _ = _guard("dependency check", _check_ripgrep)
    all_ok &= step_ok
    step_ok, tts_engine = _guard("TTS setup", lambda: _setup_tts(args, backend_dir))
    all_ok &= step_ok
    step_ok, _ = _guard("STT setup", lambda: _setup_stt(args))
    all_ok &= step_ok
    step_ok, _ = _guard("LSP setup", lambda: _setup_lsp(args))
    all_ok &= step_ok
    step_ok, _ = _guard("config", lambda: _setup_config(args, tts_engine))
    all_ok &= step_ok

    section("Done")
    if all_ok:
        ok("run `pr-walkthrough owner/repo/pull/N` to start a walkthrough")
    else:
        warn("setup finished with some steps skipped — re-run `pr-walkthrough setup` any time")
    return 0 if all_ok else 1
