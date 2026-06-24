"""In-process SSE event bus.

Each session gets a queue of SSEEvent dicts.  The SSE route reads from it;
the orchestrator pushes to it.  Queues are held in a module-level dict keyed
by session_id — ephemeral, not persisted.
"""

from __future__ import annotations

import asyncio
from typing import Any

# session_id -> list of asyncio.Queue instances (one per connected SSE client)
_queues: dict[str, list[asyncio.Queue]] = {}

_SENTINEL = object()  # signals the SSE stream to close


def subscribe(session_id: str) -> asyncio.Queue:
    """Create and register a queue for a new SSE connection."""
    q: asyncio.Queue = asyncio.Queue()
    _queues.setdefault(session_id, []).append(q)
    return q


def unsubscribe(session_id: str, q: asyncio.Queue) -> None:
    bucket = _queues.get(session_id, [])
    try:
        bucket.remove(q)
    except ValueError:
        pass


async def publish(session_id: str, event: dict[str, Any]) -> None:
    """Push an event to all SSE clients for this session."""
    for q in list(_queues.get(session_id, [])):
        await q.put(event)


async def close(session_id: str) -> None:
    """Signal all SSE clients to disconnect."""
    for q in list(_queues.get(session_id, [])):
        await q.put(_SENTINEL)


def is_sentinel(item: Any) -> bool:
    return item is _SENTINEL
