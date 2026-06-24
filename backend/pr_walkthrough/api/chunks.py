"""Chunk routes: GET /sessions/{sid}/chunks/{cid} and .../audio."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from contracts.schemas import ChunkNarration
from pr_walkthrough.orchestration import AppContext
from pr_walkthrough.orchestration.chunk_worker import process_chunk

from .deps import get_app_context

log = logging.getLogger(__name__)
router = APIRouter()

_LONG_POLL_TIMEOUT = 30.0  # seconds
_POLL_INTERVAL = 0.2

# Per (session_id, chunk_id) — coalesces concurrent requests so we don't
# fire a duplicate narration task per long-poll tick.
_inflight: set[tuple[str, str]] = set()


@router.get("/sessions/{sid}/chunks/{cid}", response_model=ChunkNarration)
async def get_chunk_narration(
    sid: str,
    cid: str,
    ctx: AppContext = Depends(get_app_context),
) -> ChunkNarration:
    """Long-poll: wait up to 30s for the chunk to be narrated, then 504.

    Triggers narration on demand if no prefetch task is in flight, so chunks
    beyond the initial prefetch (e.g. chunk 3+) get narrated when the user
    actually navigates to them.
    """
    state = _ensure_session(sid, ctx)
    _maybe_kick_off_narration(ctx, state.plan, sid, cid)
    elapsed = 0.0
    while elapsed < _LONG_POLL_TIMEOUT:
        narration = ctx.store.get_narration(sid, cid)
        if narration is not None:
            return narration
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
    raise HTTPException(status_code=504, detail=f"Chunk {cid!r} not ready within timeout")


def _maybe_kick_off_narration(ctx: AppContext, plan, sid: str, cid: str) -> None:
    if ctx.store.get_narration(sid, cid) is not None:
        return
    key = (sid, cid)
    if key in _inflight:
        return
    chunk = next((c for c in plan.chunks if c.chunk_id == cid), None)
    if chunk is None:
        return
    _inflight.add(key)

    async def _run():
        try:
            await process_chunk(ctx, plan, chunk, sid)
        finally:
            _inflight.discard(key)

    asyncio.create_task(_run(), name=f"on-demand-narrate-{sid}-{cid}")


@router.get("/sessions/{sid}/chunks/{cid}/audio")
async def get_chunk_audio(
    sid: str,
    cid: str,
    ctx: AppContext = Depends(get_app_context),
) -> StreamingResponse:
    """Stream the WAV audio for a chunk.  Falls back to live synth if not cached."""
    _ensure_session(sid, ctx)

    # Try cache first
    audio = ctx.store.get_chunk_audio(sid, cid)
    if audio is not None:
        return StreamingResponse(
            _iter_bytes(audio),
            media_type="audio/wav",
            headers={"Transfer-Encoding": "chunked"},
        )

    # Live synth from narration
    narration = ctx.store.get_narration(sid, cid)
    if narration is None:
        raise HTTPException(status_code=404, detail=f"Chunk {cid!r} narration not ready")

    async def _stream():
        chunks: list[bytes] = []
        async for wav_chunk in ctx.tts.synth(narration.narration):
            chunks.append(wav_chunk)
            yield wav_chunk
        # Cache for next time
        ctx.store.save_chunk_audio(sid, cid, b"".join(chunks))

    return StreamingResponse(
        _stream(),
        media_type="audio/wav",
        headers={"Transfer-Encoding": "chunked"},
    )


def _ensure_session(sid: str, ctx: AppContext):
    state = ctx.store.get_session_state(sid)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Session {sid!r} not found")
    return state


async def _iter_bytes(data: bytes, chunk_size: int = 65536):
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]
