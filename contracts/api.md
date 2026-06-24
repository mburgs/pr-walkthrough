# HTTP API contract

This is the surface between the frontend (stream 1) and the backend orchestrator (stream 2). Every endpoint here is also exercised by the mock backend in `fixtures/` so streams can develop in isolation.

All request and response bodies are the Pydantic models in `schemas.py`. JSON.

Errors: HTTP status + `{"detail": "..."}`. 404 for unknown session/chunk/flag, 422 for validation failures, 500 otherwise.

---

## REST

### `POST /sessions`
Start a new review session for a PR.

- **Body:** `{ "pr_url": "https://github.com/owner/repo/pull/123" }`
- **Returns:** `TourPlan`
- **Side effects:** Spawns background work to begin narrating chunk 1.

### `GET /sessions/{sid}`
Snapshot of the session.

- **Returns:** `SessionState`

### `GET /sessions/{sid}/chunks/{cid}`
Full narration for one chunk. Blocks if the chunk isn't ready yet (long-poll up to 30s, then 504).

- **Returns:** `ChunkNarration`

### `GET /sessions/{sid}/chunks/{cid}/audio`
Streamed WAV audio for the chunk's narration.

- **Returns:** `audio/wav`, chunked transfer
- **Format pin:** 22.05 kHz, 16-bit mono. First chunk MUST be a valid WAV header so playback can begin immediately.

### `POST /sessions/{sid}/follow-up`
Ask a question. Two content types supported:

- `application/json`: body is `FollowUp` with `question_text` filled
- `audio/webm` (or any audio mime): body is raw audio; backend transcribes via STT, then proceeds

- **Returns:** `FollowUpAnswer`
- **Headers:** `X-Answer-Audio-Url: /sessions/{sid}/follow-up/{aid}/audio` — fetch to stream the spoken answer

### `GET /sessions/{sid}/follow-up/{aid}/audio`
Same format as chunk audio.

### `POST /sessions/{sid}/flags`
Create a flag.

- **Body:** `Flag` without `flag_id` and `posted`
- **Returns:** `Flag` (with id assigned)

### `PATCH /sessions/{sid}/flags/{fid}`
Edit a flag (typically the `body` field as the reviewer refines wording).

- **Body:** partial `Flag` (any subset of editable fields)
- **Returns:** `Flag`

### `POST /sessions/{sid}/flags/{fid}/post`
Post the flag to the PR via `gh`. Marks `posted=true`, records `posted_url`.

- **Returns:** `Flag`

### `DELETE /sessions/{sid}/flags/{fid}`
Drop a flag. 204 on success.

---

## SSE

### `GET /sessions/{sid}/events`
Server-sent events stream. One open connection per session.

Event format follows the SSE spec:
```
event: <event_type>
data: <json>

```

Event types (full list in `events.py`):
- `chunk_started` — narration generation began
- `narration_token` — partial LLM text (progressive playback)
- `chunk_complete` — full ChunkNarration persisted; fetch via REST
- `audio_ready` — audio URL ready for fetching
- `flag_suggested` — model surfaced a concern; show inline flag prompt
- `error` — backend error, with `recoverable` boolean

A client that doesn't subscribe to SSE still works via REST polling. SSE is an optimization for low-latency first-byte playback.

---

## Audio formats (pinned)

| Direction | Format | Why |
|-----------|--------|-----|
| TTS output → frontend | WAV, 22.05 kHz, 16-bit mono | Universal browser support, low encoder complexity, Kokoro native rate |
| Frontend mic → STT | WebM/Opus (or whatever `MediaRecorder` defaults to) | Browser default with no conversion needed |

The STT adapter (`STTAdapter.transcribe`) is responsible for any format conversion server-side.

---

## Versioning

This API is **unversioned for v1**. When breakage is needed, the contracts package gets a major bump and all streams update together. There's only one client.
