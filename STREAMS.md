# Parallel development streams

The architecture splits cleanly into 8 streams. Stream 0 must land first (contracts + fixtures); streams 1–7 then run in parallel, each developing against the contracts with mock peers. Integration happens when two streams' real implementations meet.

## Stream 0 — Contracts & fixtures *(blocking prerequisite)*

Tiny stream, ~½ day. Output is in `contracts/`:

- `contracts/schemas.py` — Pydantic models, single source of truth, re-exported as JSON Schema for the frontend
- `contracts/api.md` — HTTP endpoint + SSE event catalog
- `contracts/adapters.py` — Python `Protocol` classes for LLM/TTS/STT/Context/PRSource
- `fixtures/` — at least one realistic example of every payload, so any stream can run end-to-end against mocks

Once this is merged, the other 7 streams are unblocked.

---

## Streams 1–7 (parallel)

| # | Stream | Owns | Depends on contracts from | Mocks while waiting |
|---|--------|------|---------------------------|---------------------|
| 1 | **Frontend** | `frontend/` — React app, diff viewer, narration player, side panels, follow-up UI, flag tracker | API + SSE + payload schemas | Mock backend serving `fixtures/` |
| 2 | **Backend orchestrator** | `backend/api/`, `backend/store/` — FastAPI routes, SSE, session lifecycle, SQLite | All adapter protocols | Fake adapters returning fixture data |
| 3 | **LLM / planner / narrator** | `backend/llm/`, `backend/planner/`, `backend/narrator/` — Claude calls, prompts, structured-output validation | LLM adapter protocol, TourPlan + ChunkNarration schemas | Real Claude; consumes fixture diffs |
| 4 | **TTS service** | `backend/tts/` — Kokoro adapter, Piper fallback, `say` fallback, voice selection | TTS adapter protocol | Standalone CLI: text-in → wav-out |
| 5 | **STT service** | `backend/stt/` — faster-whisper adapter, push-to-talk endpoint | STT adapter protocol | Standalone CLI: wav-in → text-out |
| 6 | **PR I/O** | `backend/diff/`, `backend/pr/` — `gh` wrappers for fetching diffs and posting comments | PRSource adapter protocol | Real `gh`; can be developed entirely standalone |
| 7 | **Context retrieval** | `backend/context/` — ripgrep-based related-code finder; LSP enrichment optional | Context adapter protocol | Standalone CLI: file:line-range → related ranges |

Streams 4, 5, 6, 7 are pure adapters with no cross-dependencies — they could all start the same day.
Streams 1, 2, 3 are the integration triangle; each is independently buildable but they're the ones that have to converge for a working demo.

---

## The contracts

### Core data types (Pydantic)

```python
# contracts/schemas.py

class Hunk(BaseModel):
    file: str
    old_range: tuple[int, int]  # (start, count)
    new_range: tuple[int, int]
    header: str                  # "@@ -45,12 +45,28 @@ def rotate_session"
    body: str                    # raw unified-diff body

class TourChunk(BaseModel):
    chunk_id: str                # stable across the session
    files: list[str]
    hunks: list[Hunk]
    summary: str                 # one-line, shown in chunk list
    rationale_for_position: str
    est_concern_level: Literal["low", "medium", "high"]

class TourPlan(BaseModel):
    session_id: str
    pr_url: str
    pr_title: str
    pr_author: str
    chunks: list[TourChunk]      # ordered

class CodeAnchor(BaseModel):
    file: str
    line_range: tuple[int, int]

class Highlight(BaseModel):
    anchor: CodeAnchor
    why: str

class RelatedCode(BaseModel):
    anchor: CodeAnchor
    relationship: Literal["definition", "callsite", "test", "prior_version", "sibling"]
    snippet: str                 # already extracted by backend

class Concern(BaseModel):
    severity: Literal["low", "medium", "high"]
    text: str
    suggested_question: str      # ready-to-post wording
    anchor: CodeAnchor | None

class ChunkNarration(BaseModel):
    chunk_id: str
    narration: str               # the spoken script
    highlights: list[Highlight]
    related_code: list[RelatedCode]
    concerns: list[Concern]
    look_closer_for: list[str]

class FollowUp(BaseModel):
    chunk_id: str | None         # current chunk context, if paused mid-tour
    question_text: str           # transcribed if voice
    transcript_confidence: float | None

class FollowUpAnswer(BaseModel):
    answer_text: str             # spoken back + shown
    new_concerns: list[Concern]  # may surface flag-worthy items
    references: list[CodeAnchor]

class Flag(BaseModel):
    flag_id: str
    chunk_id: str
    anchor: CodeAnchor | None
    severity: Literal["low", "medium", "high"]
    body: str                    # editable draft PR comment
    posted: bool
    posted_url: str | None
```

### HTTP API

```
POST   /sessions                          body: {pr_url}
                                          → TourPlan

GET    /sessions/{sid}                    → SessionState
                                            (TourPlan + flags + current chunk)

GET    /sessions/{sid}/chunks/{cid}       → ChunkNarration

GET    /sessions/{sid}/chunks/{cid}/audio → audio/wav stream
                                            (Transfer-Encoding: chunked)

POST   /sessions/{sid}/follow-up          body: {chunk_id?, text?} OR audio/*
                                          → FollowUpAnswer
                                          + audio of answer at Location header

POST   /sessions/{sid}/flags              body: Flag (without id, posted)
                                          → Flag

PATCH  /sessions/{sid}/flags/{fid}        body: partial Flag
                                          → Flag

POST   /sessions/{sid}/flags/{fid}/post   → Flag (with posted=true, url)

GET    /sessions/{sid}/events             SSE stream (see below)
```

### SSE events

Used for *progressive* narration (LLM streaming) and async backend work. Frontend can poll-only as a fallback.

```
event: chunk_started     data: {chunk_id}
event: narration_token   data: {chunk_id, text}        # partial LLM output
event: chunk_complete    data: {chunk_id}              # full ChunkNarration ready
event: audio_ready       data: {chunk_id, url}
event: flag_suggested    data: {chunk_id, concern}     # model surfaced a new one
event: error             data: {message, recoverable}
```

### Adapter protocols (Python)

```python
# contracts/adapters.py

class LLMAdapter(Protocol):
    async def plan_tour(self, pr: PRMetadata, diff: list[Hunk]) -> TourPlan: ...
    async def narrate_chunk(self, plan: TourPlan, chunk: TourChunk,
                            related: list[RelatedCode]) -> ChunkNarration: ...
    async def answer_follow_up(self, plan: TourPlan, history: list[Any],
                               q: FollowUp) -> FollowUpAnswer: ...

class TTSAdapter(Protocol):
    async def synth(self, text: str, voice: str = "default") -> AsyncIterator[bytes]:
        """Yields wav chunks (44.1kHz, 16-bit mono)."""

class STTAdapter(Protocol):
    async def transcribe(self, audio: bytes, mime: str) -> tuple[str, float]:
        """Returns (text, confidence 0–1)."""

class PRSource(Protocol):
    async def fetch(self, pr_url: str) -> tuple[PRMetadata, list[Hunk]]: ...
    async def post_comment(self, pr_url: str, body: str,
                           anchor: CodeAnchor | None) -> str:
        """Returns URL of posted comment."""

class ContextRetriever(Protocol):
    async def related(self, anchor: CodeAnchor,
                      repo_root: Path) -> list[RelatedCode]: ...
```

### Audio format (pinned to avoid stream-4-vs-stream-1 churn)

- **Synth output:** WAV, 22.05kHz, 16-bit mono, chunked HTTP transfer
- **STT input:** WebM/Opus from `MediaRecorder` (browser default); adapter handles conversion

### Fixtures required before streams 1–7 start

```
fixtures/
  pr_small/        # ~3 files, ~50 LOC change — for fast loops
    metadata.json
    diff.json      # list[Hunk]
    tour_plan.json
    chunks/c1.narration.json
    chunks/c1.audio.wav
    chunks/c2.narration.json
    follow_up_example.json
  pr_medium/       # ~10 files, ~400 LOC — realism check
    ...
```

Generation: stream 3 (real Claude) produces these once against a real PR, then commits them. Other streams treat them as static test data.

---

## Integration points

The streams converge in this order — each integration is a small PR that swaps a mock for a real implementation:

1. **3 → 2** — orchestrator stops using fake LLM adapter, calls real Claude. Demo: terminal-printed tour plan from a live PR. *(This is M1.)*
2. **6 → 2** — orchestrator stops using fixture diff, fetches via `gh`. *(Still M1.)*
3. **1 → 2** — frontend stops hitting its mock server, hits real backend. Stub narration still text-only. *(M2.)*
4. **4 → 2** — TTS plugged in, audio plays in browser. *(M3.)*
5. **5 → 2** — STT plugged in, voice follow-ups work. *(M4.)*
6. **7 → 2** — context retrieval populates side panel. *(M5.)*
7. PR posting end-to-end. *(M6.)*

---

## What this buys

- **Stream 0 (½ day) unblocks 7 streams** that can run concurrently
- **No stream is blocked on another's implementation**, only on the contract
- **Each adapter (4, 5, 6, 7) is independently demoable** via its standalone CLI before any integration
- **Frontend (1) is fully developable** against the mock backend long before LLM/TTS work
- **Contract drift is the only real risk** — guard it with: (a) Pydantic validates every payload at the wire, (b) fixtures are committed and CI loads each one to confirm schemas still parse

## Recommended day-1 order

1. Stream 0 — contracts + fixtures (single short session, do this first, no parallelism)
2. Then in parallel:
   - Streams 4 + 5 + 6 + 7 (pure adapters, low risk, demoable in isolation)
   - Stream 3 (Claude prompts — has its own iteration loop, doesn't block anyone once protocol is set)
   - Stream 1 (frontend — biggest LOC, needs the most calendar time, start ASAP)
   - Stream 2 (orchestrator — small once the adapters exist; can be last to start)
