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

# Per (session_id, chunk_id, level) → in-flight narration task. Tracking
# the task (not just a set membership flag) lets `/regenerate` cancel a
# still-running narration before it writes stale narration over the
# freshly-kicked one. Level is part of the key because multi-level mode
# runs four parallel narrations per chunk (one per familiarity level)
# and each needs its own cancel-able handle.
_inflight: dict[tuple[str, str, str], asyncio.Task] = {}

# All four levels available. Used by ALL-mode prefetch to spawn one
# worker per level when the session was created with multi_level=true.
_ALL_LEVELS = ("tutorial", "tour", "review", "highlights")


@router.get("/sessions/{sid}/chunks/{cid}", response_model=ChunkNarration)
async def get_chunk_narration(
    sid: str,
    cid: str,
    level: str | None = Query(None),
    ctx: AppContext = Depends(get_app_context),
) -> ChunkNarration:
    """Long-poll: wait up to 30s for the chunk to be narrated at the
    requested familiarity level, then 504.

    `level` defaults to the session's `plan.familiarity`. In ALL mode
    (multi_level=true) the player passes whichever level the reviewer
    has toggled to.
    """
    state = _ensure_session(sid, ctx)
    active = level or state.plan.familiarity
    _maybe_kick_off_narration(ctx, state.plan, sid, cid, level=active)
    elapsed = 0.0
    while elapsed < _LONG_POLL_TIMEOUT:
        narration = ctx.store.get_narration(sid, cid, level=active)
        if narration is not None:
            return narration
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
    raise HTTPException(status_code=504, detail=f"Chunk {cid!r}@{active} not ready within timeout")


def _maybe_kick_off_narration(
    ctx: AppContext, plan, sid: str, cid: str, level: str | None = None,
) -> None:
    """Spawn a narration task for one (chunk, level) if not already running.

    `level` defaults to the plan's configured familiarity. In ALL mode the
    sessions endpoint calls this once per level to seed all four narrations.
    """
    active = level or plan.familiarity
    if ctx.store.get_narration(sid, cid, level=active) is not None:
        return
    key = (sid, cid, active)
    if key in _inflight:
        return
    chunk = next((c for c in plan.chunks if c.chunk_id == cid), None)
    if chunk is None:
        return

    async def _run():
        try:
            await process_chunk(ctx, plan, chunk, sid, level=active)
        except asyncio.CancelledError:
            log.info("narration cancelled for %s/%s@%s", sid, cid, active)
            raise
        finally:
            # Only drop the entry if it still points at this task — if a
            # regenerate replaced us, leave the new task's entry alone.
            current = _inflight.get(key)
            if current is asyncio.current_task():
                _inflight.pop(key, None)

    task = asyncio.create_task(_run(), name=f"narrate-{sid}-{cid}-{active}")
    _inflight[key] = task


@router.get("/sessions/{sid}/files")
async def get_repo_file(
    sid: str,
    path: str,
    ctx: AppContext = Depends(get_app_context),
) -> JSONResponse:
    """Read a file from the session's `repo_root`.

    Used by the click-to-expand modal in the Related-code section, which
    needs to render the full containing file (with the relevant lines
    spotlighted) rather than only the snippet the retriever extracted.

    Path is resolved relative to the configured repo_root and must stay
    inside it — straightforward path-traversal guard.
    """
    _ensure_session(sid, ctx)
    root = ctx.repo_root.resolve()
    target = (root / path).resolve()
    try:
        rel = target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes repo root")
    # Keep dotfiles + VCS metadata out of reach. The modal only ever
    # needs source files; .git/.env/credentials should never be served.
    if any(part.startswith(".") for part in rel.parts):
        raise HTTPException(status_code=400, detail="Refusing to read dotfile path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    # Cap at 1 MB so a stray giant file (lockfile, generated sql dump) can't
    # OOM the server or freeze the modal trying to syntax-highlight it.
    if target.stat().st_size > 1_000_000:
        raise HTTPException(status_code=413, detail="File too large to preview")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse({"path": path, "content": text})


@router.get("/sessions/{sid}/chunks/{cid}/audio")
async def get_chunk_audio(
    sid: str,
    cid: str,
    level: str | None = Query(None),
    ctx: AppContext = Depends(get_app_context),
) -> StreamingResponse:
    """Stream the WAV audio for a chunk at the requested level.

    Long-polls until the chunk_worker finishes synthesising and writes
    `audio_bytes` to the store. The chunk_worker streams adapter chunks
    that aren't a single valid WAV (mix of headered + raw PCM), so we
    can't safely stream them piecemeal to the browser — we wait until
    the worker has merged + cached the final WAV, then serve that.
    """
    state = _ensure_session(sid, ctx)
    active = level or state.plan.familiarity
    _maybe_kick_off_narration_for_audio(ctx, sid, cid, level=active)

    # Long-poll for the cached final WAV
    elapsed = 0.0
    timeout = 120.0  # synth can take ~30-60s per chunk on kokoro
    poll = 0.5
    while elapsed < timeout:
        audio = ctx.store.get_chunk_audio(sid, cid, level=active)
        if audio is not None:
            return StreamingResponse(
                _iter_bytes(audio),
                media_type="audio/wav",
                headers={"Transfer-Encoding": "chunked"},
            )
        await asyncio.sleep(poll)
        elapsed += poll

    raise HTTPException(status_code=504, detail=f"Audio for {cid!r} not ready within timeout")


@router.post("/sessions/{sid}/chunks/{cid}/regenerate")
async def regenerate_chunk(
    sid: str,
    cid: str,
    ctx: AppContext = Depends(get_app_context),
) -> JSONResponse:
    """Wipe the chunk's narration + audio + variants and re-kick the worker.

    Useful when iterating on the narrate prompt — the client polls
    /chunks/{cid} (which long-polls until the new narration arrives).
    """
    state = _ensure_session(sid, ctx)
    chunk = next((c for c in state.plan.chunks if c.chunk_id == cid), None)
    if chunk is None:
        raise HTTPException(status_code=404, detail=f"Chunk {cid!r} not in plan")
    ctx.store.delete_chunk_cache(sid, cid)
    # Cancel any in-flight narration tasks for this chunk (across all
    # levels) before kicking the new one. In single-level sessions only
    # one entry will be present; in ALL mode there may be up to four.
    to_cancel = [k for k in _inflight if k[0] == sid and k[1] == cid]
    for k in to_cancel:
        prev = _inflight.pop(k, None)
        if prev is not None and not prev.done():
            prev.cancel()
            try:
                await prev
            except (asyncio.CancelledError, Exception):
                pass
    # Re-kick: if the session is multi_level, re-seed all four levels.
    levels = _ALL_LEVELS if state.plan.multi_level else (state.plan.familiarity,)
    for lvl in levels:
        _maybe_kick_off_narration(ctx, state.plan, sid, cid, level=lvl)
    return JSONResponse({"status": "regenerating", "chunk_id": cid})


def _maybe_kick_off_narration_for_audio(
    ctx: AppContext, sid: str, cid: str, level: str | None = None,
) -> None:
    """If the chunk hasn't been narrated yet, kick off the narration task.

    Same coalescing logic as the narration endpoint — without this, hitting
    /audio for a not-yet-narrated chunk would wait forever.
    """
    state = ctx.store.get_session_state(sid)
    if state is None:
        return
    active = level or state.plan.familiarity
    if ctx.store.get_chunk_audio(sid, cid, level=active) is not None:
        return
    _maybe_kick_off_narration(ctx, state.plan, sid, cid, level=active)


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
    # Variant synth runs the same heavy TTS pipeline as process_chunk; gate
    # it on the same semaphore so a burst of variant requests can't bypass
    # the cap and blow past available RAM.
    async with ctx.tts_semaphore:
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
