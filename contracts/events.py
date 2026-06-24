"""SSE event types for /sessions/{sid}/events.

The frontend subscribes once per session; events are pushed as backend
work completes. Each event is a Pydantic model; serialized as JSON in the
SSE `data:` field, with the model's `event_type` literal as the SSE
`event:` field.

Fallback: a frontend that doesn't open the SSE stream still works via
polling the REST endpoints — SSE is an optimization, not a requirement.
"""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel

from contracts.schemas import Concern


class ChunkStartedEvent(BaseModel):
    event_type: Literal["chunk_started"] = "chunk_started"
    chunk_id: str


class NarrationTokenEvent(BaseModel):
    """Partial LLM output. Sent as the planner streams the narration field
    so the frontend can begin TTS / display before the full chunk lands."""

    event_type: Literal["narration_token"] = "narration_token"
    chunk_id: str
    text: str


class ChunkCompleteEvent(BaseModel):
    """Full ChunkNarration is now persisted; fetch via REST."""

    event_type: Literal["chunk_complete"] = "chunk_complete"
    chunk_id: str


class AudioReadyEvent(BaseModel):
    event_type: Literal["audio_ready"] = "audio_ready"
    chunk_id: str
    url: str  # GET this URL to stream the audio


class FlagSuggestedEvent(BaseModel):
    """Model surfaced a concern worth flagging."""

    event_type: Literal["flag_suggested"] = "flag_suggested"
    chunk_id: str
    concern: Concern


class ErrorEvent(BaseModel):
    event_type: Literal["error"] = "error"
    message: str
    recoverable: bool


SSEEvent = Union[
    ChunkStartedEvent,
    NarrationTokenEvent,
    ChunkCompleteEvent,
    AudioReadyEvent,
    FlagSuggestedEvent,
    ErrorEvent,
]
