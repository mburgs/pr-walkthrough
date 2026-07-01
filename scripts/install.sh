#!/usr/bin/env bash
# pr-walkthrough installer.
#
#   curl -fsSL https://raw.githubusercontent.com/mburgs/pr-walkthrough/main/scripts/install.sh | bash
#
# Clones the repo, creates a venv, installs the Python + frontend
# dependencies, and links `pr-walkthrough` onto PATH. Safe to re-run —
# it updates the existing checkout and refreshes dependencies instead
# of starting over.
#
# Env overrides:
#   PR_WALKTHROUGH_HOME     where to clone the source (default ~/.pr-walkthrough)
#   PR_WALKTHROUGH_BIN_DIR  where to link the CLI onto PATH (default ~/.local/bin)

set -euo pipefail

REPO_URL="https://github.com/mburgs/pr-walkthrough"
INSTALL_DIR="${PR_WALKTHROUGH_HOME:-$HOME/.pr-walkthrough}"
BIN_DIR="${PR_WALKTHROUGH_BIN_DIR:-$HOME/.local/bin}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'
  c_dim=$'\033[2m'; c_bold=$'\033[1m'; c_reset=$'\033[0m'
else
  c_ok=""; c_warn=""; c_err=""; c_dim=""; c_bold=""; c_reset=""
fi

step()    { printf '  %s→%s %s\n' "$c_bold" "$c_reset" "$1"; }
ok()      { printf '  %s✓%s %s\n' "$c_ok" "$c_reset" "$1"; }
warn()    { printf '  %s!%s %s\n' "$c_warn" "$c_reset" "$1" >&2; }
section() { printf '\n%s%s%s\n' "$c_bold" "$1" "$c_reset"; }
fail() {
  printf '  %s✗%s %s\n' "$c_err" "$c_reset" "$1" >&2
  exit 1
}

# Run a command quietly; only show its output (dimmed) if it fails, so
# the happy path reads as a handful of clean status lines instead of
# raw git/pip/npm noise.
run_or_fail() {
  local desc="$1"; shift
  local log
  if log=$("$@" 2>&1); then
    return 0
  fi
  printf '  %s✗%s %s\n' "$c_err" "$c_reset" "$desc" >&2
  printf '%s%s%s\n' "$c_dim" "$log" "$c_reset" >&2
  exit 1
}

section "pr-walkthrough installer"

command -v git    >/dev/null 2>&1 || fail "git is required — install it and re-run"
command -v python3 >/dev/null 2>&1 || fail "python3 (3.11+) is required — install it and re-run"
command -v node   >/dev/null 2>&1 || fail "node (20+) is required — install it and re-run"
command -v npm    >/dev/null 2>&1 || fail "npm is required — install it and re-run"

case "$(uname -s)" in
  Darwin|Linux) : ;;
  *) fail "unsupported OS — pr-walkthrough supports macOS and Linux" ;;
esac

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)'; then
  fail "python 3.11+ required, found $(python3 --version 2>&1)"
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  step "updating existing install at $INSTALL_DIR"
  if ! git -C "$INSTALL_DIR" pull --ff-only --quiet origin main >/dev/null 2>&1; then
    warn "couldn't fast-forward $INSTALL_DIR (local changes?) — using it as-is"
  fi
else
  step "cloning into $INSTALL_DIR"
  run_or_fail "git clone failed" git clone --quiet "$REPO_URL" "$INSTALL_DIR"
fi
ok "source ready at $INSTALL_DIR"

if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  step "creating virtualenv"
  run_or_fail "virtualenv creation failed" python3 -m venv "$INSTALL_DIR/.venv"
fi

step "installing Python dependencies (this can take a minute)…"
run_or_fail "pip upgrade failed" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
run_or_fail "pip install failed" "$INSTALL_DIR/.venv/bin/pip" install --quiet \
  -e "$INSTALL_DIR/backend" -e "$INSTALL_DIR"
ok "python dependencies installed"

step "installing frontend dependencies…"
run_or_fail "npm install failed" npm --prefix "$INSTALL_DIR/frontend" install --silent
ok "frontend dependencies installed"

mkdir -p "$BIN_DIR"
for exe in pr-walkthrough pr-walkthrough-stt pr-context; do
  ln -sf "$INSTALL_DIR/.venv/bin/$exe" "$BIN_DIR/$exe"
done
ok "linked pr-walkthrough into $BIN_DIR"

section "Done"
ok "pr-walkthrough installed"

if ! command -v pr-walkthrough >/dev/null 2>&1; then
  warn "$BIN_DIR isn't on your PATH yet — add this to your shell profile:"
  printf '%s      export PATH="%s:$PATH"%s\n' "$c_dim" "$BIN_DIR" "$c_reset"
fi

printf '\n  Next:\n\n'
printf '    export ANTHROPIC_API_KEY=sk-ant-...\n'
printf '    pr-walkthrough setup    %s# optional: better TTS voice, STT model, language servers%s\n' "$c_dim" "$c_reset"
printf '\n'
