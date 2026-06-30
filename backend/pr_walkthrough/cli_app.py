"""`pr-walkthrough` CLI entry point.

Replaces the old `scripts/serve` launcher. Responsibilities:

    1. Parse a PR ref (owner/repo/pull/N or full GitHub URL).
    2. Load global + repo config; create the global config on first run
       with the local cache opted in.
    3. Prompt for `--familiarity` if not provided.
    4. Spawn the backend (uvicorn) and frontend (vite) as subprocesses,
       pick free ports, wire CORS + VITE_BACKEND_URL.
    5. Wait for `/healthz`, POST `/sessions`, open the browser at the
       frontend's `#session=<sid>` URL so the reviewer lands directly
       in the session shell — no homepage form needed.
    6. Forward only important log lines (PR fetched, plan ready, chunk N
       phase changes, errors) to the user's terminal; suppress the rest.
       Errors always print.
    7. Clean up both subprocesses on Ctrl-C.

The CLI is *the* entry point now. `scripts/serve` stays for legacy use
during the transition but the README points here.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import IO

import httpx

from pr_walkthrough import config as cfg_mod

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- args


_FAMILIARITY_LEVELS = ("tutorial", "tour", "review", "highlights")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pr-walkthrough",
        description="Guided PR review with narration. Launches the local web UI.",
    )
    p.add_argument(
        "pr",
        nargs="?",
        help="PR reference — owner/repo/pull/N shorthand or full https://github.com/... URL",
    )
    p.add_argument(
        "--familiarity",
        choices=(*_FAMILIARITY_LEVELS, "all"),
        help="Narration verbosity. Omit for interactive prompt; 'all' generates every level.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PR_WALKTHROUGH_PORT") or 0) or None,
        help="Pin the backend port (default: pick a free one)",
    )
    p.add_argument(
        "--frontend-port",
        type=int,
        default=int(os.environ.get("PR_WALKTHROUGH_FRONTEND_PORT") or 0) or None,
        help="Pin the frontend port (default: pick a free one)",
    )
    p.add_argument(
        "--repos-dir",
        type=Path,
        default=Path(os.environ.get("PR_WALKTHROUGH_REPOS_DIR") or Path.home() / "code"),
        help="Parent dir holding checkouts as subdirs; repo is resolved from the PR slug",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        help="Don't open the browser automatically — useful for headless testing.",
    )
    return p.parse_args(argv)


# --------------------------------------------------------------------------- pr ref


_PR_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<num>\d+)",
)
_PR_SHORT_RE = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<num>\d+)/?$",
)


def parse_pr_ref(ref: str) -> str:
    """Accepts either a full GitHub URL or `owner/repo/pull/N` shorthand;
    returns the canonical https URL the rest of the pipeline expects.

    Raises ValueError on garbage input — the CLI prints the error and
    exits before starting any subprocesses, so the user sees a clean
    failure instead of a backend traceback."""
    ref = ref.strip()
    m = _PR_URL_RE.match(ref) or _PR_SHORT_RE.match(ref)
    if not m:
        raise ValueError(
            f"can't parse PR ref {ref!r} — expected owner/repo/pull/N or full GitHub URL"
        )
    return f"https://github.com/{m['owner']}/{m['repo']}/pull/{m['num']}"


# --------------------------------------------------------------------------- prompts


def _gh_pr_files(pr_url: str) -> list[str]:
    """Pre-flight: ask `gh` for the PR's file list so we can detect
    languages before the backend even boots. Cheap (~500 ms) and lets
    us offer LSP install upfront rather than after the user's waited
    for plan_tour. Returns [] on any failure — we degrade gracefully."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "files", "-q", ".files[].path"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return []
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _check_and_offer_lsp_install(pr_url: str) -> None:
    """Inspect the PR's languages; for any missing LSP server, prompt
    the user to install it. Confirmed installs run in the foreground.
    Non-TTY (CI) silently skips the prompt — the backend still works
    via ripgrep fallback.

    Implementation note: re-checking PATH after install picks up the
    new binary for the backend subprocess because we inherit env, and
    `shutil.which()` re-walks PATH each call."""
    from pr_walkthrough.context.lsp.detect import (
        install_hint, language_for_files, resolve_server_command,
    )
    files = _gh_pr_files(pr_url)
    if not files:
        return
    languages = language_for_files(files)
    missing = [lang for lang in languages if resolve_server_command(lang) is None]
    if not missing:
        return
    _print_section(f"This PR touches {', '.join(sorted(languages))}")
    print(_S.dim("    LSP gives precise find-references; ripgrep is the fallback"), flush=True)
    for lang in sorted(missing):
        hint = install_hint(lang)
        if not hint:
            continue
        if not sys.stdin.isatty():
            _print_warn(f"{lang}: missing LSP. To install: {hint}")
            continue
        ans = input(f"    Install LSP for {lang}? Run `{_S.bold(hint)}` [Y/n] ").strip().lower()
        if ans in ("", "y", "yes"):
            _print_step(f"installing {lang} LSP…")
            try:
                # Capture so package-manager noise doesn't drown the CLI;
                # surface stderr only on failure.
                result = subprocess.run(
                    hint.split(), capture_output=True, text=True, timeout=300,
                )
                if result.returncode == 0:
                    _print_ok(f"installed {lang} LSP")
                else:
                    _print_err(f"install failed for {lang}; continuing without LSP")
                    if result.stderr.strip():
                        print(_S.dim(result.stderr.strip()), file=sys.stderr, flush=True)
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                _print_err(f"install failed ({e}); continuing without LSP for {lang}")


def prompt_familiarity() -> str:
    """Interactive prompt when --familiarity wasn't passed and stdin is
    a TTY. Non-TTY (CI, pipes) defaults to 'review' silently."""
    if not sys.stdin.isatty():
        return "review"
    print("\nFamiliarity level controls narration detail:")
    print("  1) tutorial   — most detailed, beginner-friendly")
    print("  2) tour       — guided walkthrough")
    print("  3) review     — terse code-review focus  [default]")
    print("  4) highlights — fastest pass, only the key changes")
    print("  5) all        — generate every level (multi-level mode)")
    while True:
        choice = input("Choose [1-5, default 3]: ").strip() or "3"
        mapping = dict(zip("12345", (*_FAMILIARITY_LEVELS, "all")))
        if choice in mapping:
            return mapping[choice]
        print(f"  → {choice!r} not understood; pick 1-5")


# --------------------------------------------------------------------------- ports


def pick_port() -> int:
    """Bind ephemerally to find a free port then release it. Race-prone
    in theory; in practice fine for a single-user dev launcher."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------- styling


class _Style:
    """ANSI colour helpers. No-ops when stdout isn't a TTY so piped /
    redirected output stays clean."""

    def __init__(self) -> None:
        enable = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self._on = enable

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._on else text

    def ok(self, t: str) -> str:    return self._wrap("32", t)           # green
    def warn(self, t: str) -> str:  return self._wrap("33", t)           # yellow
    def err(self, t: str) -> str:   return self._wrap("31", t)           # red
    def arrow(self, t: str) -> str: return self._wrap("36", t)           # cyan
    def dim(self, t: str) -> str:   return self._wrap("2", t)
    def bold(self, t: str) -> str:  return self._wrap("1", t)


_S = _Style()


def _print_step(label: str) -> None:
    print(f"  {_S.arrow('→')} {label}", flush=True)


def _print_ok(label: str) -> None:
    print(f"  {_S.ok('✓')} {label}", flush=True)


def _print_warn(label: str) -> None:
    print(f"  {_S.warn('!')} {label}", flush=True)


def _print_err(label: str) -> None:
    print(f"  {_S.err('✗')} {label}", file=sys.stderr, flush=True)


def _print_section(label: str) -> None:
    print(f"\n{_S.bold(label)}", flush=True)


# --------------------------------------------------------------------------- log filter


# Patterns that map subprocess output to clean status updates in our
# own voice. Suppressed entirely means "we print our own line for this
# milestone, no need to forward the raw backend log too".
_SUPPRESS_PATTERNS = (
    re.compile(r"Uvicorn running on"),             # we print "backend ready" ourselves
    re.compile(r"Application startup complete"),
    re.compile(r"Started server process"),
    re.compile(r"Waiting for application startup"),
    re.compile(r"Started reloader process"),
    re.compile(r"\bWatchFiles detected changes"),
    re.compile(r"VITE\s+v[\d.]+"),                 # we print "frontend ready" ourselves
    re.compile(r"ready in \d+ ms"),
    re.compile(r"\bLocal:\s+http"),
    re.compile(r"\bNetwork:"),
    re.compile(r"^\s*press h \+ enter"),
    # Suppress per-chunk debug noise — useful for verbose mode only
    re.compile(r"\bcache hit\b"),
    re.compile(r"chunk worker"),
)

# Recognized backend progress markers — converted to CLI status updates.
# Backend emits these via `log.info("progress: ...")` from key milestones.
_PROGRESS_PATTERNS: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"progress:\s*fetching PR"),          "step", "fetching PR diff"),
    (re.compile(r"progress:\s*PR fetched\s*(.*)$"),   "ok",   "PR fetched {1}"),
    (re.compile(r"progress:\s*planning tour"),        "step", "planning tour"),
    (re.compile(r"progress:\s*tour ready\s*(.*)$"),   "ok",   "tour ready {1}"),
    (re.compile(r"spawning LSP server\s+(\S+)\s+for\s+(\S+)"),
                                                      "step", "spawning LSP ({2})"),
    (re.compile(r"retriever:\s*LSP for\s+(\S+)"),     "ok",   "LSP active for {1}"),
    (re.compile(r"retriever:\s*ripgrep fallback for\s+(\S+).*?(install [^.]+)\.?"),
                                                      "warn", "no LSP for {1} — install: {2}"),
)

_ERROR_HINTS = (
    "ERROR", "CRITICAL", "Exception", "Traceback", "raise", "error:",
)


def _is_error_line(line: str) -> bool:
    return any(hint in line for hint in _ERROR_HINTS)


def _is_suppressed(line: str) -> bool:
    return any(p.search(line) for p in _SUPPRESS_PATTERNS)


def _strip_log_prefix(line: str) -> str:
    """Trim uvicorn/python log prefixes so the message reads cleanly.

    Examples handled:
      "[be] INFO:     Uvicorn running on..."   -> "Uvicorn running on..."
      "2026-01-01 12:00:00,123 logger INFO message" -> "message"
    """
    # Strip uvicorn-style "INFO:     " prefix
    line = re.sub(r"^(INFO|WARNING|ERROR|DEBUG|CRITICAL):\s+", "", line)
    # Strip "2026-... logger INFO " timestamp prefix
    line = re.sub(
        r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,.]\d+\s+\S+\s+(INFO|WARNING|ERROR|DEBUG|CRITICAL)\s+",
        "", line,
    )
    return line


def _format_template(template: str, match: re.Match) -> str:
    """Substitute {1}, {2}, … with regex capture groups. Empty captures
    are silently dropped so the output reads naturally."""
    out = template
    for i, g in enumerate(match.groups(), start=1):
        out = out.replace(f"{{{i}}}", (g or "").strip())
    return out.strip()


def _forwarder(stream: IO[bytes], verbose: bool) -> None:
    """Read a subprocess pipe and translate recognized lines to CLI
    status updates; suppress noise; let errors through verbatim."""
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        clean = _strip_log_prefix(line)

        # 1. Recognized progress markers → CLI voice
        matched = False
        for pat, kind, tmpl in _PROGRESS_PATTERNS:
            m = pat.search(clean)
            if m is None:
                continue
            msg = _format_template(tmpl, m)
            if kind == "ok":   _print_ok(msg)
            elif kind == "warn": _print_warn(msg)
            else:               _print_step(msg)
            matched = True
            break
        if matched:
            continue

        # 2. Errors pass through with red prefix so the user notices
        if _is_error_line(clean):
            _print_err(clean)
            continue

        # 3. Otherwise: only show in verbose mode; suppress known boot
        # noise we already represent with our own status lines.
        if verbose and not _is_suppressed(clean):
            print(_S.dim(f"  · {clean}"), flush=True)


# --------------------------------------------------------------------------- repo paths


def repo_root_from_args(args: argparse.Namespace, pr_url: str) -> Path:
    """Derive the on-disk repo root the same way the backend will, so
    config files land in the right place."""
    m = _PR_URL_RE.match(pr_url)
    assert m, "pr_url already canonicalised"
    repo_name = m["repo"]
    candidate = args.repos_dir / repo_name
    return candidate if candidate.is_dir() else args.repos_dir


# --------------------------------------------------------------------------- main


def _find_venv_bin(repo_root: Path, exe: str) -> str:
    """Find an installed binary in a `.venv` — main repo first, then any
    sibling worktree's venv (so a shared venv across worktrees works).
    Falls back to the bare binary name (PATH lookup) if no venv found."""
    candidates = [repo_root / ".venv" / "bin" / exe]
    candidates.extend(p / exe for p in (repo_root / ".claude" / "worktrees").glob("*/.venv/bin"))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return exe


def _warn_if_no_kokoro() -> None:
    """Loudly nudge the user toward Kokoro if it isn't installed.

    The fallback chain is kokoro -> piper -> say; macOS `say` works
    everywhere but sounds noticeably synthetic. Most users want kokoro
    but skip the optional extra by accident on first install. Checked
    via the adapter's own `is_available()` so this stays in sync with
    the actual selection logic in `make_tts()`.
    """
    try:
        from pr_walkthrough.tts.kokoro_adapter import KokoroTTSAdapter
        if KokoroTTSAdapter.is_available():
            return
    except Exception:
        pass  # treat any import failure as "not available"
    _print_warn("Kokoro TTS not installed — falling back to macOS `say` (robotic)")
    print(_S.dim("    install: pip install -e 'backend[kokoro]'  (~300 MB on first run)"), flush=True)


def _wait_for_health(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def _wait_for_url(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def _create_session(backend_port: int, pr_url: str, familiarity: str) -> str:
    """POST /sessions and return the session_id."""
    multi = familiarity == "all"
    body = {
        "pr_url": pr_url,
        "familiarity": "review" if multi else familiarity,
        "multi_level": multi,
    }
    # Plan_tour can take a while on big PRs — generous timeout.
    r = httpx.post(
        f"http://127.0.0.1:{backend_port}/sessions",
        json=body,
        timeout=300.0,
    )
    r.raise_for_status()
    return r.json()["session_id"]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
    )
    args = _parse_args(argv)

    # 1. PR ref
    pr = args.pr
    if not pr:
        if not sys.stdin.isatty():
            print("error: PR ref required (owner/repo/pull/N or URL)", file=sys.stderr)
            return 2
        pr = input("PR (owner/repo/pull/N or URL): ").strip()
    try:
        pr_url = parse_pr_ref(pr)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # 2. Config + repo dir
    repo_root = repo_root_from_args(args, pr_url)
    global_cfg = cfg_mod.ensure_global_config_exists()
    cfg = cfg_mod.load_config(repo_root)

    # 3. LSP install offer (pre-flight, before backend boots so new
    # binaries are on PATH when uvicorn spawns)
    _check_and_offer_lsp_install(pr_url)

    # 4. Familiarity
    familiarity = args.familiarity or cfg.familiarity or prompt_familiarity()
    if familiarity not in (*_FAMILIARITY_LEVELS, "all"):
        print(f"error: invalid familiarity {familiarity!r}", file=sys.stderr)
        return 2

    # 4. Resolve repo + script locations
    cli_path = Path(__file__).resolve()
    backend_dir = cli_path.parent.parent  # backend/
    project_root = backend_dir.parent     # repo root
    frontend_dir = project_root / "frontend"

    # Allow being run from a worktree — the script is inside the worktree.
    uvicorn_exe = _find_venv_bin(project_root, "uvicorn")
    npm_exe = "npm"

    backend_port = args.port or pick_port()
    frontend_port = args.frontend_port or pick_port()

    # 5. Env wiring
    env = os.environ.copy()
    env["PR_WALKTHROUGH_ALLOWED_ORIGINS"] = (
        f"http://localhost:{frontend_port},http://127.0.0.1:{frontend_port}"
    )
    env["VITE_BACKEND_URL"] = f"http://127.0.0.1:{backend_port}"
    env["PR_WALKTHROUGH_REPOS_DIR"] = str(args.repos_dir)
    env["PYTHONPATH"] = f"{project_root}:{backend_dir}" + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
    )
    # Cache opt-in from config — the backend reads PR_WALKTHROUGH_CACHE
    # from the env to decide whether to attach a PersistentCache to its
    # AppContext. (See deps.py.)
    if global_cfg.cache.enabled:
        env["PR_WALKTHROUGH_CACHE"] = "1"
        env["PR_WALKTHROUGH_CACHE_MAX_GB"] = str(global_cfg.cache.max_gb)

    # 5b. TTS engine check. Kokoro produces much better narration than
    # macOS `say`; warn early when it's not installed so the user
    # doesn't sit through a session of robot voice and wonder why.
    _warn_if_no_kokoro()

    # 6. Frontend deps
    if not (frontend_dir / "node_modules" / ".bin" / "vite").exists():
        _print_step("installing frontend deps (one-time)…")
        result = subprocess.run(
            ["npm", "install"], cwd=frontend_dir,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            _print_err("npm install failed:")
            print(result.stderr or result.stdout, file=sys.stderr)
            return 1
        _print_ok("frontend deps installed")

    # 7. Spawn backend + frontend
    _print_section("Starting services")
    _print_step(f"backend  http://127.0.0.1:{backend_port}")
    _print_step(f"frontend http://localhost:{frontend_port}")

    backend = subprocess.Popen(
        [uvicorn_exe, "pr_walkthrough.main:app",
         "--host", "127.0.0.1", "--port", str(backend_port)],
        cwd=backend_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    frontend = subprocess.Popen(
        [npm_exe, "run", "dev", "--",
         "--port", str(frontend_port), "--strictPort"],
        cwd=frontend_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    verbose = bool(os.environ.get("PR_WALKTHROUGH_VERBOSE"))
    threading.Thread(
        target=_forwarder, args=(backend.stdout, verbose), daemon=True,
    ).start()
    threading.Thread(
        target=_forwarder, args=(frontend.stdout, verbose), daemon=True,
    ).start()

    def _shutdown(*_args) -> None:
        for p in (backend, frontend):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in (backend, frontend):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    signal.signal(signal.SIGINT, lambda *_: (_shutdown(), sys.exit(130)))
    signal.signal(signal.SIGTERM, lambda *_: (_shutdown(), sys.exit(143)))

    # 8. Wait for backend, create session, open browser
    if not _wait_for_health(backend_port, timeout=60.0):
        _print_err("backend didn't come up in 60s")
        _shutdown()
        return 1
    _print_ok("backend ready")
    _print_ok("frontend ready")

    _print_section(f"Building tour for {pr_url}")
    try:
        sid = _create_session(backend_port, pr_url, familiarity)
    except httpx.HTTPError as e:
        _print_err(f"session creation failed: {e}")
        _shutdown()
        return 1
    _print_ok(f"session ready ({sid})")

    target = f"http://localhost:{frontend_port}/#session={sid}"
    if not _wait_for_url(f"http://localhost:{frontend_port}/", timeout=30.0):
        _print_warn("frontend dev server slow; opening anyway")
    _print_step(f"opening {_S.bold(target)}")
    if not args.no_open:
        webbrowser.open(target)
    print(_S.dim("\n  Ctrl-C to stop"), flush=True)

    # 9. Supervise until either child dies
    try:
        while backend.poll() is None and frontend.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    _shutdown()
    return 0
