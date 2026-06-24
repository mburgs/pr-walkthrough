"""SSE endpoint: GET /sessions/{sid}/events."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from pr_walkthrough.orchestration import AppContext
from pr_walkthrough.orchestration import event_bus

from .deps import get_app_context

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/sessions/{sid}/events")
async def session_events(
    sid: str,
    ctx: AppContext = Depends(get_app_context),
) -> StreamingResponse:
    """SSE stream for a session.  One open connection per client."""
    state = ctx.store.get_session_state(sid)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Session {sid!r} not found")

    q = event_bus.subscribe(sid)

    async def _generate():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # heartbeat comment to keep connection alive
                    yield ": heartbeat\n\n"
                    continue

                if event_bus.is_sentinel(item):
                    break

                event_type = item.get("event_type", "message")
                data = json.dumps({k: v for k, v in item.items() if k != "event_type"})
                yield f"event: {event_type}\ndata: {data}\n\n"
        finally:
            event_bus.unsubscribe(sid, q)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
