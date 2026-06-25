"""Session routes: POST /sessions, GET /sessions/{sid}."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from contracts.schemas import SessionState, TourPlan
from pr_walkthrough.orchestration import AppContext
from pr_walkthrough.orchestration.chunk_worker import process_chunk

from .deps import get_app_context

log = logging.getLogger(__name__)
router = APIRouter()


class CreateSessionRequest(BaseModel):
    pr_url: str


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
    plan = plan.model_copy(update={"session_id": f"sess_{uuid.uuid4().hex[:12]}"})

    ctx.store.create_session(plan)

    # Kick off background narration for chunk 1 (and prefetch chunk 2)
    if plan.chunks:
        asyncio.create_task(
            process_chunk(ctx, plan, plan.chunks[0], plan.session_id),
            name=f"narrate-{plan.session_id}-{plan.chunks[0].chunk_id}",
        )
    if len(plan.chunks) > 1:
        asyncio.create_task(
            process_chunk(ctx, plan, plan.chunks[1], plan.session_id),
            name=f"narrate-{plan.session_id}-{plan.chunks[1].chunk_id}",
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
