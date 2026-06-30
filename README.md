# pr-walkthrough

A narrated, browser-based walkthrough of a GitHub pull request.
Hand it a PR ref; an LLM plans a tour of the diff and talks you
through it — reading the relevant code aloud, highlighting the lines
it's discussing, surfacing related code from the repo, and capturing
concerns you can post back as review comments.

Audio and transcription run locally. Only the diff text leaves the
machine, and only to Anthropic.

## Install

Prereqs: Python 3.11+, Node 20+, `gh` authed, an Anthropic API key,
macOS or Linux.

```bash
git clone https://github.com/mburgs/pr-walkthrough && cd pr-walkthrough
python -m venv .venv && source .venv/bin/activate
pip install -e backend -e .
(cd frontend && npm install)
```

Optional: install [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)
for higher-quality TTS (`pip install -e 'backend[kokoro]'`; ~300 MB of
weights download on first run). Without it, macOS `say` is the fallback.

Set the API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
pr-walkthrough owner/repo/pull/N
```

Accepts the shorthand above or the full URL. The CLI starts the
backend + frontend, waits for the plan, then opens the browser
straight into the session — no homepage form. Ctrl-C kills both.

Flags:

```
--familiarity {tutorial,tour,review,highlights,all}   omit for interactive prompt
--port / --frontend-port                              pin ports (default: pick free)
--repos-dir DIR                                       parent of local checkouts (default ~/code)
--no-open                                             skip opening the browser
```

For cross-repo context retrieval to find references, the PR's repo
needs to be checked out under `--repos-dir` (the CLI resolves the
repo from the URL slug — `owner/calsync/pull/6` → `<repos-dir>/calsync`).

## Config

Two TOML files, both optional, both written on first run when needed:

```
~/.config/pr-walkthrough/config.toml      global defaults
<repo>/.pr-walkthrough/config.toml        per-repo overrides (gitignored)
```

The CLI writes the global file on first launch with the persistent
narration + TTS cache enabled — every run on the same `head_sha`
skips the LLM + TTS round-trip and reuses cached output. Cache lives
at `~/.cache/pr-walkthrough/cache.db`, LRU-capped at 1 GB. Editing
the prompt template invalidates downstream rows automatically (the
key includes a hash of `pr_walkthrough/llm/prompts.py`).

To disable caching: set `[cache] enabled = false` in the global
config, or unset `PR_WALKTHROUGH_CACHE` in the env.

## Stack

| Layer        | Choice                                                       |
|--------------|--------------------------------------------------------------|
| Backend      | Python 3.11+, FastAPI, SQLite                                |
| Frontend     | Vite + React 19 + TypeScript                                 |
| LLM          | Claude (Sonnet) — planning + structured narration            |
| Context      | LSP (pyright / typescript-language-server) → ripgrep fallback |
| TTS          | Kokoro / Piper / macOS `say` (selectable)                    |
| STT          | Parakeet via MLX (push-to-talk follow-ups; Apple Silicon)    |
| PR I/O       | `gh` CLI (your existing auth)                                |

## Develop

```bash
# Backend
cd backend && pytest

# Frontend e2e (MSW-mocked backend; no Python needed)
cd frontend && npx playwright install --with-deps   # first time only
cd frontend && npx playwright test

# Frontend typecheck
cd frontend && npx tsc --noEmit
```

For frontend-only iteration without spinning up the CLI: `cd frontend
&& npm run dev` — Vite serves the SPA, MSW intercepts every backend
call, and `/` shows the empty state until you visit
`/#session=sess_pr_small_001` (the canonical MSW fixture).
