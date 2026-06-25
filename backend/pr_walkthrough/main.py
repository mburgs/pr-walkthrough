"""FastAPI application entry point.

Run:
    cd backend && uvicorn pr_walkthrough.main:app --reload

The default AppContext wires up all fakes, so no external services are needed.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pr_walkthrough.api import deps
from pr_walkthrough.api.sessions import router as sessions_router
from pr_walkthrough.api.chunks import router as chunks_router
from pr_walkthrough.api.follow_ups import router as follow_ups_router
from pr_walkthrough.api.flags import router as flags_router
from pr_walkthrough.api.events import router as events_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)

app = FastAPI(
    title="pr-walkthrough",
    version="0.1.0",
    description="Guided code-review API",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions_router)
app.include_router(chunks_router)
app.include_router(follow_ups_router)
app.include_router(flags_router)
app.include_router(events_router)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Return 500 as a normal response so CORSMiddleware adds its headers.

    Without this, Starlette's ServerErrorMiddleware sits outside the CORS
    middleware and bare 500s reach the browser with no Access-Control-Allow-Origin —
    which surfaces as a confusing CORS error instead of the real exception.
    """
    logging.getLogger("pr_walkthrough").exception("unhandled exception on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


@app.on_event("startup")
async def _startup() -> None:
    """Eagerly initialise the AppContext singleton on startup."""
    deps.get_app_context()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
