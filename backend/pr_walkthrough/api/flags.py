"""Flag routes: CRUD + post-to-PR."""

from __future__ import annotations

import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from typing import Any

from contracts.schemas import CodeAnchor, Flag, Severity
from pr_walkthrough.orchestration import AppContext

from .deps import get_app_context

log = logging.getLogger(__name__)
router = APIRouter()


class CreateFlagBody(BaseModel):
    """POST /sessions/{sid}/flags body — Flag without flag_id and posted."""

    chunk_id: str
    anchor: CodeAnchor | None = None
    severity: Severity
    body: str


class PatchFlagBody(BaseModel):
    """PATCH body — any subset of editable Flag fields."""

    anchor: CodeAnchor | None = None
    severity: Severity | None = None
    body: str | None = None


@router.post("/sessions/{sid}/flags", response_model=Flag, status_code=201)
async def create_flag(
    sid: str,
    body: CreateFlagBody,
    ctx: AppContext = Depends(get_app_context),
) -> Flag:
    _ensure_session(sid, ctx)
    flag = Flag(
        flag_id=f"flag_{uuid.uuid4().hex[:8]}",
        chunk_id=body.chunk_id,
        anchor=body.anchor,
        severity=body.severity,
        body=body.body,
        posted=False,
        posted_url=None,
    )
    return ctx.store.create_flag(sid, flag)


@router.patch("/sessions/{sid}/flags/{fid}", response_model=Flag)
async def patch_flag(
    sid: str,
    fid: str,
    body: PatchFlagBody,
    ctx: AppContext = Depends(get_app_context),
) -> Flag:
    flag = _get_flag_or_404(sid, fid, ctx)
    updates = body.model_dump(exclude_none=True)
    updated = flag.model_copy(update=updates)
    return ctx.store.update_flag(sid, updated)


@router.post("/sessions/{sid}/flags/{fid}/post", response_model=Flag)
async def post_flag_to_pr(
    sid: str,
    fid: str,
    ctx: AppContext = Depends(get_app_context),
) -> Flag:
    state = ctx.store.get_session_state(sid)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Session {sid!r} not found")
    flag = _get_flag_or_404(sid, fid, ctx)
    if flag.posted:
        return flag

    comment_url = await ctx.pr_source.post_comment(
        state.plan.pr.url,
        flag.body,
        flag.anchor,
    )
    updated = flag.model_copy(update={"posted": True, "posted_url": comment_url})
    return ctx.store.update_flag(sid, updated)


@router.delete("/sessions/{sid}/flags/{fid}", status_code=204)
async def delete_flag(
    sid: str,
    fid: str,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    _ensure_session(sid, ctx)
    deleted = ctx.store.delete_flag(sid, fid)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Flag {fid!r} not found")
    return Response(status_code=204)


def _ensure_session(sid: str, ctx: AppContext) -> None:
    if ctx.store.get_session_state(sid) is None:
        raise HTTPException(status_code=404, detail=f"Session {sid!r} not found")


def _get_flag_or_404(sid: str, fid: str, ctx: AppContext) -> Flag:
    _ensure_session(sid, ctx)
    flag = ctx.store.get_flag(sid, fid)
    if flag is None:
        raise HTTPException(status_code=404, detail=f"Flag {fid!r} not found")
    return flag
