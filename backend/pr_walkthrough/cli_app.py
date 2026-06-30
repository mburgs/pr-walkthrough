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


# --------------------------------------------------------------------------- log filter


# Lines that should bubble up to the user's terminal. Everything else
# stays hidden unless --verbose. Errors / exceptions always print
# regardless of the filter list — see `_is_error_line`.
_SURFACE_PATTERNS = (
    re.compile(r"pr_source.*fetch"),
    re.compile(r"\bplan_tour\b"),
    re.compile(r"chunk worker"),
    re.compile(r"\bcache hit\b"),
    re.compile(r"Uvicorn running on"),
    re.compile(r"Application startup complete"),
    re.compile(r"VITE\s+v[\d.]+"),
    re.compile(r"ready in \d+ ms"),
)

_ERROR_HINTS = (
    "ERROR", "CRITICAL", "Exception", "Traceback", "raise", "error:",
)


def _is_error_line(line: str) -> bool:
    return any(hint in line for hint in _ERROR_HINTS)


def _is_surface_line(line: str) -> bool:
    return any(p.search(line) for p in _SURFACE_PATTERNS)


def _tee(stream: IO[bytes], prefix: str, verbose: bool) -> None:
    """Read a subprocess pipe in a background thread, forward filtered
    lines to stdout. Errors always pass through; surface patterns pass
    in non-verbose mode; everything passes in verbose mode."""
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        if verbose or _is_error_line(line) or _is_surface_line(line):
            print(f"{prefix} {line}", flush=True)


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
    but skip the optional extra by accident on first install. We
    check via the adapter's own `is_available()` so this stays in
    sync with the actual selection logic in `make_tts()`.
    """
    try:
        from pr_walkthrough.tts.kokoro_adapter import KokoroTTSAdapter
        if KokoroTTSAdapter.is_available():
            return
    except Exception:
        pass  # treat any import failure as "not available"
    # ANSI yellow if the terminal looks like it'll handle it; the
    # subprocess output forwarding already strips colour codes from
    # non-tty parents so this is safe.
    bold, dim, reset = ("\033[1;33m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("", "", "")
    print(
        f"{bold}! Kokoro TTS not installed — falling back to macOS `say`.{reset}\n"
        f"{dim}  `say` sounds robotic; install Kokoro for usable narration:{reset}\n"
        f"{dim}    pip install -e 'backend[kokoro]'{reset}\n"
        f"{dim}  (one-time ~300 MB model download on first run){reset}\n",
        flush=True,
    )


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

    # 3. Familiarity
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
        print("→ installing frontend deps (one-time)…", flush=True)
        subprocess.check_call(["npm", "install"], cwd=frontend_dir)

    # 7. Spawn backend + frontend
    print(f"→ backend  http://127.0.0.1:{backend_port}", flush=True)
    print(f"→ frontend http://localhost:{frontend_port}", flush=True)

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
        target=_tee, args=(backend.stdout, "[be]", verbose), daemon=True,
    ).start()
    threading.Thread(
        target=_tee, args=(frontend.stdout, "[fe]", verbose), daemon=True,
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
        print("error: backend didn't come up in 60s", file=sys.stderr)
        _shutdown()
        return 1
    print("✓ backend ready", flush=True)

    print(f"→ fetching PR + planning tour for {pr_url}…", flush=True)
    try:
        sid = _create_session(backend_port, pr_url, familiarity)
    except httpx.HTTPError as e:
        print(f"error: session creation failed: {e}", file=sys.stderr)
        _shutdown()
        return 1
    print(f"✓ session {sid}", flush=True)

    target = f"http://localhost:{frontend_port}/#session={sid}"
    if not _wait_for_url(f"http://localhost:{frontend_port}/", timeout=30.0):
        print("warning: frontend dev server slow; opening anyway", flush=True)
    print(f"→ opening {target}", flush=True)
    if not args.no_open:
        webbrowser.open(target)

    # 9. Supervise until either child dies
    try:
        while backend.poll() is None and frontend.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    _shutdown()
    return 0
