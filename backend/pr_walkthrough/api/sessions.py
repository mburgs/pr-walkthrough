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


def _log_retriever_tiers(ctx: AppContext, plan: TourPlan) -> None:
    """Log one line per language present in the PR with the active
    retriever tier and (if not LSP) an install hint. Reads the pool
    state off `ctx.context` when it's the LSP-aware hybrid; silent for
    fakes / custom retrievers."""
    from pr_walkthrough.context.lsp.detect import (
        install_hint, language_for_files,
    )
    pool = getattr(getattr(ctx, "context", None), "_pool", None)
    files = [f for chunk in plan.chunks for f in chunk.files]
    languages = language_for_files(files)
    if not languages:
        return
    for lang in sorted(languages):
        if pool is not None and pool.is_available(lang):
            log.info("retriever: LSP for %s (precise references)", lang)
        else:
            hint = install_hint(lang)
            if hint:
                log.warning(
                    "retriever: ripgrep fallback for %s — install LSP for "
                    "better results: %s",
                    lang, hint,
                )
            else:
                log.info("retriever: ripgrep for %s (no LSP supported yet)", lang)


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

    # Emit progress markers the CLI's log forwarder turns into clean
    # status updates. Without these, the only thing the user sees during
    # the ~30s plan_tour call is silence.
    log.info("progress: fetching PR %s", body.pr_url)
    metadata, hunks = await ctx.pr_source.fetch(body.pr_url)
    log.info(
        "progress: PR fetched (%d file%s, %d hunk%s)",
        len({h.file for h in hunks}), "s" if len({h.file for h in hunks}) != 1 else "",
        len(hunks), "s" if len(hunks) != 1 else "",
    )

    # Cache hit skips the ~20s plan_tour LLM call. Keyed by (repo,
    # head_sha, prompt_version) so a re-run on the same revision loads
    # instantly and a new push (new head_sha) or prompt change misses.
    plan = None
    cache = getattr(ctx, "cache", None)
    plan_key = None
    if cache is not None:
        from pr_walkthrough.cache import tour_plan_cache_key
        plan_key = tour_plan_cache_key(metadata.repo, metadata.head_sha)
        plan = cache.get_tour_plan(plan_key)
    if plan is not None:
        log.info(
            "progress: tour ready from cache (%d chunk%s)",
            len(plan.chunks), "s" if len(plan.chunks) != 1 else "",
        )
    else:
        log.info("progress: planning tour")
        plan = await ctx.llm.plan_tour(metadata, hunks)
        if cache is not None and plan_key is not None:
            try:
                cache.put_tour_plan(plan_key, plan)
            except Exception:
                log.warning("failed to persist tour plan to cache", exc_info=True)
        log.info(
            "progress: tour ready (%d chunk%s)",
            len(plan.chunks), "s" if len(plan.chunks) != 1 else "",
        )

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

    # Announce which retriever tier each language in the PR will use,
    # so the CLI surface filter can show the user (and tell them what
    # to install to upgrade). Cheap: just walks the chunk file list.
    _log_retriever_tiers(ctx, plan)

    # Sliding-window prefetch: kick off chunk 1 (all 4 levels in ALL mode,
    # else just the active level) plus chunk 2 at the active level. Chunks
    # 3+ are kicked lazily by GET /chunks/:cid as the reviewer advances —
    # see _maybe_prefetch_next in api/chunks.py.
    #
    # Why narrow: each chunk now runs two LLM calls (narration + anchor
    # pass) inside the same llm_semaphore slot, so wide prefetch on a
    # multi-chunk PR pegs the semaphore for ~minutes and the active
    # chunk's TTS waits behind dozens of background tasks. The sliding
    # window keeps 2 ahead "in the oven" — fast enough to feel
    # prefetched, narrow enough that the active chunk stays first in
    # the queue.
    levels_for_first = _ALL_LEVELS if plan.multi_level else (plan.familiarity,)
    if plan.chunks:
        for lvl in levels_for_first:
            _maybe_kick_off_narration(ctx, plan, plan.session_id, plan.chunks[0].chunk_id, level=lvl)
    if len(plan.chunks) > 1:
        _maybe_kick_off_narration(
            ctx, plan, plan.session_id, plan.chunks[1].chunk_id,
            level=plan.familiarity,
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
