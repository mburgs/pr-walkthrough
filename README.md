# pr-walkthrough

A narrated, browser-based code-review companion for GitHub pull requests. Open a PR URL and an LLM walks you through the diff in a sensible order — speaking aloud, highlighting the lines it's discussing, surfacing related code, and capturing concerns you can post back as review comments.

The narrator follows a **tour plan** instead of file-order so the architectural shape of the change comes first; chunks are grouped by purpose (API surface · Mechanism · Tests · …) in the sidebar. Audio synthesis, transcription, and PR I/O all run locally — only the diff text leaves the machine, and only to Anthropic.

## Status

Working end-to-end. The PR demo flow (URL → plan → narrated tour with audio + diff highlighting + flag capture) is the canonical test. 110+ backend tests and 13 Playwright e2e tests cover the surface.

## Stack

| Layer | Choice |
|-------|--------|
| Backend | Python 3.11+, FastAPI, SQLite |
| Frontend | Vite + React 19 + TypeScript |
| Diff render | `react-diff-view` with refractor (Prism) tokens |
| LLM | Claude (Sonnet) via `anthropic` SDK — planning + structured narration |
| Cross-repo context | Jedi (Python static analysis) + ripgrep fallback |
| TTS | Kokoro 82M (local, Apache-2.0) — also Piper and macOS `say` |
| STT | `faster-whisper` (push-to-talk follow-ups) |
| PR I/O | `gh` CLI (uses your existing auth) |

## Quickstart

### Prerequisites

- Python 3.11+
- Node 20+
- `gh` CLI, authenticated (`gh auth status`)
- An Anthropic API key
- macOS or Linux

### Install

```bash
# Backend (editable; also installs the shared `contracts` package from the repo root)
cd backend
python -m venv ../.venv && source ../.venv/bin/activate
pip install -e . -e ..

# Frontend
cd ../frontend
npm install
```

By default the backend installs without any TTS extra and falls back to macOS `say` if no engine is registered. To install Kokoro (recommended on Apple Silicon):

```bash
pip install -e '.[kokoro]'   # ~300 MB of model weights download on first run
```

### Configure

Required:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Optional but useful — point cross-repo context retrieval at the local clone of the PR's repo so Jedi can resolve references:

```bash
export PR_WALKTHROUGH_REPO_ROOT=/path/to/local/clone
```

CORS defaults to the Vite dev server (`http://localhost:5173`). Override if you serve the frontend elsewhere:

```bash
export PR_WALKTHROUGH_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

### Run

Two terminals:

```bash
# Terminal 1 — backend
cd backend && uvicorn pr_walkthrough.main:app --reload --port 8200

# Terminal 2 — frontend
cd frontend && npm run dev
```

Open `http://localhost:5173`, paste a PR URL into the homepage form, and the walkthrough renders.

To deep-link past the form: `http://localhost:5173/?pr=https://github.com/owner/repo/pull/123`.

## How it works

```
┌─ Frontend (React) ────────────────────────────────────────┐
│  DiffViewer  ·  NarrationPlayer  ·  RightRail (related,   │
│  concerns, flags)  ·  FollowUpInput  ·  RelatedCodeModal  │
└──────────────────┬────────────────────────────────────────┘
                   │ HTTP + SSE (long-polled per chunk)
┌──────────────────▼────────────────────────────────────────┐
│  Backend (FastAPI)                                         │
│  ┌─────────────┐ ┌───────────────┐ ┌──────────────────┐   │
│  │ PRSource    │ │ TourPlanner   │ │ ChunkNarrator    │   │
│  │ (gh CLI)    │ │ (Claude)      │ │ (Claude, tools)  │   │
│  └─────────────┘ └───────────────┘ └──────────────────┘   │
│  ┌─────────────┐ ┌───────────────┐ ┌──────────────────┐   │
│  │ ContextRet  │ │ TTS adapter   │ │ STT adapter      │   │
│  │ (Jedi + rg) │ │ (Kokoro/say)  │ │ (faster-whisper) │   │
│  └─────────────┘ └───────────────┘ └──────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ SessionStore (SQLite with FK cascades)              │  │
│  └─────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

The planner picks a tour order driven by *architectural altitude* — what changes the API surface lands first, then the mechanism, then tests — and a chunk can split across files or repeat a hunk if narrative cohesion warrants it. Each chunk's narration is structured (`segments[]` with optional anchors, plus `related_code`, `concerns`, and audio-segment offsets) so the UI can highlight lines while the audio plays.

When you click a related-code row in the right rail, the modal fetches the **full containing file** from the configured `repo_root` and scrolls to the anchor range. Path-traversal-protected, 1 MB cap, dotfiles refused.

### Data egress

The diff text goes to Anthropic (Claude). **Audio never leaves the machine** — TTS synth and STT transcription both run locally. PR I/O uses your local `gh` auth.

## Development

```bash
# Backend tests
cd backend && pytest

# E2E (frontend + MSW-mocked backend; no real Python backend needed)
cd frontend && npx playwright install --with-deps   # first time only
cd frontend && npx playwright test

# Frontend typecheck
cd frontend && npx tsc --noEmit
```

Test layout:

- `backend/tests/test_chunks_endpoints.py` — long-poll, audio variants, regenerate, `/files`
- `backend/tests/test_narration_pipeline.py` — `tts_scrub`, anchor coercion, `_snap_anchors_to_chunk_hunks`
- `backend/tests/test_e2e.py` — end-to-end FastAPI surface against fake adapters
- `backend/tests/pr/test_diff_parser.py` — unified diff parsing edge cases
- `backend/tests/test_llm_adapter.py` — opt-in live Claude tests (marked `live`; needs `ANTHROPIC_API_KEY`)
- `frontend/e2e/walkthrough.spec.ts` — Playwright over MSW

## Project layout

```
pr-walkthrough/
├── backend/
│   └── pr_walkthrough/
│       ├── api/            # FastAPI routers (sessions, chunks, flags, follow_ups, events)
│       ├── context/        # Jedi + ripgrep retrievers for related code
│       ├── fakes/          # in-process fakes for tests / dev
│       ├── llm/            # Claude adapter, prompts, tool-use schemas
│       ├── orchestration/  # AppContext, chunk_worker (narrate + synth pipeline)
│       ├── pr/             # gh CLI source, unified diff parser
│       ├── store/          # SQLite session store
│       ├── stt/            # faster-whisper adapter
│       ├── tts/            # Kokoro / Piper / say adapters
│       └── main.py         # FastAPI app entry
├── contracts/              # shared schemas + adapter Protocols (Pydantic)
├── frontend/
│   └── src/
│       ├── api/            # typed client
│       ├── components/     # DiffViewer, NarrationPlayer, RightRail, RelatedCodeModal, …
│       ├── contexts/       # SessionContext (state machine + lifecycle)
│       ├── mocks/          # MSW handlers + fixtures
│       └── lib/            # transcript export, syntax highlight wrapper
└── fixtures/               # canonical PR-shaped fixtures
```

## Design notes

[`STREAMS.md`](STREAMS.md) captures the 8-stream parallel-development decomposition the project started from, including the contracts that let frontend/backend/LLM/TTS/STT/PR teams iterate independently.

## Acknowledgements

Diff rendering by [`react-diff-view`](https://github.com/otakustay/react-diff-view), syntax highlighting via [refractor](https://github.com/wooorm/refractor) (Prism). Local TTS by [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M). Code intelligence by [Jedi](https://github.com/davidhalter/jedi). PR I/O via [GitHub CLI](https://cli.github.com/).
