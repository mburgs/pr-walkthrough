# pr-walkthrough

A guided code-review companion. Point it at a PR (or local branch diff), and an LLM narrator walks you through the change as if the author were giving the tour — speaking aloud, highlighting code, surfacing related context, and capturing questions you want to post back to the PR.

Status: **planning**. Nothing implemented yet.

## Why

Code reviews are getting harder to do well. The friction isn't reading the diff — it's reconstructing context, deciding what deserves attention, and remembering to write the question down before it slips. A narrated walkthrough offloads the pacing and the context-gathering, and the question tracker keeps the review output durable.

## Core capabilities

- Browser UI that shows pieces of the diff with surrounding context
- TTS narration walking through each chunk in a sensible order (not strictly file-by-file)
- Related code pulled in automatically (definitions, callers, recent history)
- Pause-and-ask follow-up questions (text or voice)
- Question tracker: items get drafted as PR comments, posted via `gh` when ready
- Compliance mode: local-only TTS/STT/LLM for work use

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Frontend (Vite + React + TS)                           │
│  - DiffViewer (file tree + hunks, react-diff-view)      │
│  - NarrationPlayer (play/pause/skip/replay chunk)       │
│  - SidePanel: related code, AI annotations, flags       │
│  - QuestionTracker (mark-for-PR, edit, post)            │
│  - FollowUpInput (text + push-to-talk mic)              │
└──────────────────┬──────────────────────────────────────┘
                   │ SSE (events) + HTTP (audio, RPC)
┌──────────────────▼──────────────────────────────────────┐
│  Backend (FastAPI)                                      │
│  ┌────────────┐ ┌──────────────┐ ┌──────────────────┐  │
│  │ DiffSource │ │ TourPlanner  │ │ ChunkNarrator    │  │
│  │ (gh / git) │ │ (LLM)        │ │ (LLM, structured)│  │
│  └────────────┘ └──────────────┘ └──────────────────┘  │
│  ┌────────────┐ ┌──────────────┐ ┌──────────────────┐  │
│  │ ContextRet │ │ TTS adapter  │ │ STT adapter      │  │
│  │ (LSP/rg)   │ │ (Kokoro/...) │ │ (faster-whisper) │  │
│  └────────────┘ └──────────────┘ └──────────────────┘  │
│  ┌──────────────────────────────────────────────────┐  │
│  │ SessionStore (SQLite): plan, chunks, flags, Q&A  │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## Default stack (negotiable)

| Layer | Choice | Why |
|-------|--------|-----|
| Backend | Python + FastAPI | Best ML/audio ecosystem; clean async + SSE |
| Frontend | Vite + React + TS | Standard, fast iteration |
| Diff render | `react-diff-view` | Hunk-level control, side-by-side or unified |
| TTS (local) | Kokoro 82M (Apache-2.0) | Best quality-per-MB local TTS; CPU-fine on Mac |
| TTS fallback | Piper, then macOS `say` | Always something working |
| STT (local) | faster-whisper (base / small) | CTranslate2 backend, fast on CPU |
| LLM | Claude API (work + personal access) | Best planning + structured output; audio stays local so code-only egress is fine |
| Storage | SQLite | One file, zero ops |
| PR I/O | `gh` CLI shelled out | Auth already handled; fine to lean on it where convenient |

## Key flows

### 1. Plan generation (once per session)
Fetch diff → group hunks by file → LLM emits an ordered tour:
```json
[
  { "chunk_id": "c1",
    "files": ["src/auth/session.py"],
    "hunks": ["@@ -45,12 +45,28 @@", "..."],
    "summary": "New session-token rotation logic",
    "rationale_for_position": "Architecturally central; everything else builds on this",
    "est_concern_level": "medium" }
]
```
The order matters — lead with the architecturally interesting parts, not alphabetical filenames.

### 2. Per-chunk narration (lazy, with prefetch of N+1)
LLM produces **structured output** per chunk:
```json
{
  "narration": "We're swapping the in-memory session map for...",
  "code_highlights": [{"file": "...", "line_range": [50, 62], "why": "..."}],
  "related_code":    [{"file": "...", "line_range": [10, 30], "relationship": "callsite"}],
  "concerns":        [{"severity": "medium", "text": "...", "suggested_question": "..."}],
  "look_closer_for": ["race between rotate() and read()"]
}
```
- `narration` → TTS → streamed audio chunks
- Everything else → SSE events that drive UI panels

### 3. Follow-up Q&A
- Pause → ask (text or push-to-talk)
- LLM has session context: tour plan + chunks seen + current code + prior Q&A
- If model agrees the concern is real, an **"Add to PR questions"** button surfaces

### 4. Question tracker → PR
- Persistent per-session list
- Each item: free text + anchor (file:line) + draft comment body
- "Post to PR" → `gh pr review --comment` or inline via `gh api`

## Data egress posture

Code goes to Claude (approved at work and personal). **Audio never leaves the machine** — TTS synth and STT transcription both run locally. That's the entire compliance story; no separate "compliance mode" needed.

A startup audit logs every outbound destination the first time it's used, so it's obvious if a dependency starts phoning home unexpectedly.

## Decisions locked in (2026-06-23)

- **LLM:** Claude across the board
- **Diff source:** GitHub PRs (no local-branch flow in v1)
- **Voice follow-ups:** required for v1
- **Standalone tool**, but free to lean on `gh` CLI for PR fetch + comment posting since it removes an auth layer
- **Language-agnostic:** no per-language code paths; LSP enrichment (M5) can grow language support opportunistically

## Parallel development

See [STREAMS.md](STREAMS.md) for the 8-stream decomposition, the contracts that make parallelism possible, and the recommended day-1 order. The milestones below describe *integration points* between streams, not sequential phases.

## Milestone sketch

1. **M1 — Walking skeleton:** `gh pr view --json` → Claude tour plan → terminal-printed. No UI, no TTS. Proves the planning prompt against real PRs.
2. **M2 — UI shell:** React app renders diff + chunk list, plays through a stubbed narration endpoint (text-only bubbles, no audio yet).
3. **M3 — TTS online:** Kokoro adapter behind `/tts`, audio streams per chunk, prefetch N+1.
4. **M4 — Voice follow-ups:** faster-whisper push-to-talk → Claude with full session context → reply rendered + spoken. (Pulled forward from M7 since you flagged it as v1-required.)
5. **M5 — Context retrieval:** related-code side panel via ripgrep (+ optional LSP later).
6. **M6 — Question tracker + PR posting:** flag concerns → draft → `gh pr review --comment` / inline via `gh api`.
7. **M7 — Polish:** keyboard shortcuts, replay/skip, session resume, transcript export.

## Repo layout (planned)

```
pr-walkthrough/
  backend/
    pyproject.toml
    pr_walkthrough/
      api/           # FastAPI routes + SSE
      diff/          # gh + local git sources
      planner/       # tour planning LLM calls
      narrator/      # per-chunk structured narration
      context/       # related-code retrieval
      tts/           # adapters: kokoro, piper, say
      stt/           # adapters: faster-whisper
      llm/           # adapters: anthropic, ollama
      store/         # sqlite session persistence
  frontend/
    package.json
    src/
      components/
      hooks/
      api/
  PLAN.md            # this file's longer-form thinking, evolves
  README.md          # this file
```
