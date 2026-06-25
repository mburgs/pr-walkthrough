"""Chunk routes: GET /sessions/{sid}/chunks/{cid} and .../audio."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from contracts.schemas import ChunkNarration
from pr_walkthrough.orchestration import AppContext
from pr_walkthrough.orchestration.chunk_worker import (
    process_chunk, tts_scrub, synth_segments_to_wav,
)

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
    """Stream the WAV audio for a chunk.

    Long-polls until the chunk_worker finishes synthesising and writes
    `audio_bytes` to the store. The chunk_worker streams adapter chunks
    that aren't a single valid WAV (mix of headered + raw PCM), so we
    can't safely stream them piecemeal to the browser — we wait until
    the worker has merged + cached the final WAV, then serve that.
    """
    _ensure_session(sid, ctx)
    _maybe_kick_off_narration_for_audio(ctx, sid, cid)

    # Long-poll for the cached final WAV
    elapsed = 0.0
    timeout = 120.0  # synth can take ~30-60s per chunk on kokoro
    poll = 0.5
    while elapsed < timeout:
        audio = ctx.store.get_chunk_audio(sid, cid)
        if audio is not None:
            return StreamingResponse(
                _iter_bytes(audio),
                media_type="audio/wav",
                headers={"Transfer-Encoding": "chunked"},
            )
        await asyncio.sleep(poll)
        elapsed += poll

    raise HTTPException(status_code=504, detail=f"Audio for {cid!r} not ready within timeout")


def _maybe_kick_off_narration_for_audio(ctx: AppContext, sid: str, cid: str) -> None:
    """If the chunk hasn't been narrated yet, kick off the narration task.

    Same coalescing logic as the narration endpoint — without this, hitting
    /audio for a not-yet-narrated chunk would wait forever.
    """
    if ctx.store.get_chunk_audio(sid, cid) is not None:
        return
    state = ctx.store.get_session_state(sid)
    if state is None:
        return
    _maybe_kick_off_narration(ctx, state.plan, sid, cid)


def _ensure_session(sid: str, ctx: AppContext):
    state = ctx.store.get_session_state(sid)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Session {sid!r} not found")
    return state


async def _iter_bytes(data: bytes, chunk_size: int = 65536):
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


# ------------------------------------------------------------------ variants

# (sid, cid, engine, filtered) → in-flight task. Coalesces concurrent
# variant requests so we synth each combo at most once.
_variant_inflight: dict[tuple[str, str, str, bool], asyncio.Task] = {}

_VARIANT_TIMEOUT = 900.0  # First-time XTTS download is ~2GB; F5 is ~1GB
_VARIANT_POLL = 0.5


@router.get("/sessions/{sid}/chunks/{cid}/audio/variants")
async def list_variants(
    sid: str,
    cid: str,
    ctx: AppContext = Depends(get_app_context),
) -> JSONResponse:
    """List engines available + variants already cached for this chunk.

    The frontend renders the engine + filter switchers based on `engines`
    and shows which combinations have audio ready (vs. need on-demand synth).
    """
    _ensure_session(sid, ctx)
    engines = (
        ctx.tts_registry.known() if ctx.tts_registry else []
    )
    cached = ctx.store.list_audio_variants(sid, cid)
    return JSONResponse({
        "engines": engines,
        "cached": [
            {"engine": e, "filtered": f} for (e, f) in cached
        ],
    })


@router.get("/sessions/{sid}/chunks/{cid}/audio.variant")
async def get_audio_variant(
    sid: str,
    cid: str,
    engine: str = Query(...),
    filtered: bool = Query(True),
    ctx: AppContext = Depends(get_app_context),
) -> StreamingResponse:
    """Serve a specific (engine, filtered) audio variant for this chunk.

    Synthesises on demand if not cached. The cumulative segment offsets
    are sent via the `X-Segment-Offsets-Ms` header (JSON-encoded list[int])
    so the player can drive the diff highlight against this variant's
    timings — engines produce different segment durations.
    """
    _ensure_session(sid, ctx)
    narration = ctx.store.get_narration(sid, cid)
    if narration is None:
        raise HTTPException(status_code=404, detail=f"Chunk {cid!r} narration not ready")

    cached = ctx.store.get_audio_variant(sid, cid, engine, filtered)
    if cached is not None:
        audio, offsets = cached
        return _audio_response(audio, offsets)

    # Not cached — synth on demand, coalesced with any concurrent request
    if ctx.tts_registry is None or engine not in ctx.tts_registry.known():
        raise HTTPException(status_code=400, detail=f"Unknown engine: {engine!r}")

    key = (sid, cid, engine, filtered)
    if key not in _variant_inflight:
        _variant_inflight[key] = asyncio.create_task(
            _synth_variant(ctx, sid, cid, engine, filtered, narration.segments),
            name=f"synth-variant-{engine}-{'f' if filtered else 'r'}-{cid}",
        )

    try:
        await asyncio.wait_for(asyncio.shield(_variant_inflight[key]), timeout=_VARIANT_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Variant synth timed out for {engine}/{cid}")
    finally:
        # Drop completed/failed task from registry
        if key in _variant_inflight and _variant_inflight[key].done():
            del _variant_inflight[key]

    again = ctx.store.get_audio_variant(sid, cid, engine, filtered)
    if again is None:
        raise HTTPException(status_code=500, detail="Variant synth completed without storing audio")
    audio, offsets = again
    return _audio_response(audio, offsets)


async def _synth_variant(
    ctx: AppContext,
    sid: str,
    cid: str,
    engine: str,
    filtered: bool,
    segments,
) -> None:
    """Run TTS for one variant and persist the result + offsets.

    The first call for an engine triggers model construction (XTTS-v2 is
    ~2GB, F5-TTS ~1GB). That work is sync — push it to a thread so we
    don't block the FastAPI event loop and starve other in-flight requests.
    """
    log.info("synth variant %s/%s/filtered=%s for %s", engine, cid, filtered, sid)
    tts = await asyncio.to_thread(ctx.tts_registry.get, engine)
    texts = [tts_scrub(s.text) if filtered else s.text for s in segments]
    audio, offsets = await synth_segments_to_wav(tts, texts)
    ctx.store.save_audio_variant(sid, cid, engine, filtered, audio, offsets)


def _audio_response(audio: bytes, offsets: list[int]) -> StreamingResponse:
    return StreamingResponse(
        _iter_bytes(audio),
        media_type="audio/wav",
        headers={
            "Transfer-Encoding": "chunked",
            "X-Segment-Offsets-Ms": json.dumps(offsets),
            "Access-Control-Expose-Headers": "X-Segment-Offsets-Ms",
        },
    )
