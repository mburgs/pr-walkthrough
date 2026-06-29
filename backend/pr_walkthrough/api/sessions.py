"""Session routes: POST /sessions, GET /sessions/{sid}."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from contracts.schemas import FamiliarityLevel, SessionState, TourPlan
from pr_walkthrough.orchestration import AppContext

from .chunks import _ALL_LEVELS, _maybe_kick_off_narration
from .deps import get_app_context

log = logging.getLogger(__name__)
router = APIRouter()


class CreateSessionRequest(BaseModel):
    pr_url: str
    familiarity: FamiliarityLevel = "review"
    multi_level: bool = False


@router.post("/sessions", response_model=TourPlan, status_code=201)
async def create_session(
    body: CreateSessionRequest,
    ctx: AppContext = Depends(get_app_context),
) -> TourPlan:
    """Fetch PR, plan tour, persist, spawn chunk-1 narration in background."""
    import uuid

    metadata, hunks = await ctx.pr_source.fetch(body.pr_url)
    plan = await ctx.llm.plan_tour(metadata, hunks)

    # The LLM populates session_id in its structured output, but it can pick a
    # deterministic-looking ID (e.g. "sess_cli_cli_pr1") which collides on
    # repeat POSTs — React StrictMode's dev double-effect alone tripped UNIQUE.
    # Override with a server-generated UUID; the orchestrator owns identity.
    # Familiarity comes from the client, not the planner; stamp it here so
    # the narration step downstream can read it off the plan.
    plan = plan.model_copy(update={
        "session_id": f"sess_{uuid.uuid4().hex[:12]}",
        "familiarity": body.familiarity,
        "multi_level": body.multi_level,
    })

    ctx.store.create_session(plan)

    # Kick off background narration for every chunk at the active level so
    # the reviewer doesn't pay the LLM+TTS latency one chunk at a time.
    # Concurrency is gated by `llm_semaphore` / `tts_semaphore`, so blasting
    # a 10-chunk PR doesn't actually run 10 LLM calls or 10 TTS synths in
    # parallel — it queues them behind the configured cap (defaults: 8 LLM,
    # 1–4 TTS depending on detected RAM). Result: while the reviewer reads
    # chunk N, chunks N+1…N+k are already being prepared.
    #
    # In ALL mode the first chunk gets all four familiarity levels (so the
    # level switcher feels instant); later chunks still get only the active
    # level prefetched, with the other three lazily generated on demand.
    levels_for_first = _ALL_LEVELS if plan.multi_level else (plan.familiarity,)
    if plan.chunks:
        for lvl in levels_for_first:
            _maybe_kick_off_narration(ctx, plan, plan.session_id, plan.chunks[0].chunk_id, level=lvl)
        for chunk in plan.chunks[1:]:
            _maybe_kick_off_narration(
                ctx, plan, plan.session_id, chunk.chunk_id, level=plan.familiarity,
            )

    return plan


@router.get("/sessions/{sid}", response_model=SessionState)
async def get_session(
    sid: str,
    ctx: AppContext = Depends(get_app_context),
) -> SessionState:
    state = ctx.store.get_session_state(sid)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Session {sid!r} not found")
    return state
