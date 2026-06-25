"""Shared contracts for pr-walkthrough.

Every cross-stream payload, adapter signature, and API event lives here.
Streams 1-7 import from this package; the package is the seam.

Do not add stream-specific logic. If something is only used by one stream,
it doesn't belong here.
"""

from contracts.schemas import (
    CodeAnchor,
    ChunkNarration,
    Concern,
    Flag,
    FollowUp,
    FollowUpAnswer,
    Hunk,
    NarrationSegment,
    PRMetadata,
    RelatedCode,
    SessionState,
    TourChunk,
    TourPlan,
)
from contracts.adapters import (
    ContextRetriever,
    LLMAdapter,
    PRSource,
    STTAdapter,
    TTSAdapter,
)
from contracts.events import (
    SSEEvent,
    ChunkStartedEvent,
    NarrationTokenEvent,
    ChunkCompleteEvent,
    AudioReadyEvent,
    FlagSuggestedEvent,
    ErrorEvent,
)

__all__ = [
    "CodeAnchor",
    "ChunkNarration",
    "Concern",
    "Flag",
    "FollowUp",
    "FollowUpAnswer",
    "Hunk",
    "NarrationSegment",
    "PRMetadata",
    "RelatedCode",
    "SessionState",
    "TourChunk",
    "TourPlan",
    "ContextRetriever",
    "LLMAdapter",
    "PRSource",
    "STTAdapter",
    "TTSAdapter",
    "SSEEvent",
    "ChunkStartedEvent",
    "NarrationTokenEvent",
    "ChunkCompleteEvent",
    "AudioReadyEvent",
    "FlagSuggestedEvent",
    "ErrorEvent",
]
