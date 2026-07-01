"""Data contracts shared across every stream.

These Pydantic models are the wire format for the HTTP API, the SSE stream,
and the LLM structured-output schemas. Treat them as load-bearing — every
change is a coordinated cross-stream change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high"]
RelationshipKind = Literal[
    "definition", "callsite", "test", "prior_version", "sibling"
]

# Verbosity gradient for the narration. Picked by the reviewer at session
# create time and threaded into the narrate prompt. Each higher level
# *adds* coverage on top of the lower one — Tutorial is the most detailed,
# Highlights is the most terse.
#
#   tutorial   — also explains language/framework constructs the reviewer
#                may not know (Python decorators, async patterns, etc.)
#   tour       — adds repo context: how this PR fits the codebase, what
#                patterns are conventional vs. new
#   review     — focuses on the change; assumes repo familiarity
#   highlights — just the high-impact moments (current default behaviour)
FamiliarityLevel = Literal["tutorial", "tour", "review", "highlights"]


class PRMetadata(BaseModel):
    """Surface-level info about the PR being reviewed."""

    url: str
    repo: str  # "owner/name"
    number: int
    title: str
    author: str
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    body: str = ""


class Hunk(BaseModel):
    """A single contiguous diff hunk inside one file."""

    file: str
    old_range: tuple[int, int]  # (start_line, line_count); 0,0 for added files
    new_range: tuple[int, int]
    header: str  # full @@ header line incl. function context
    body: str  # raw unified-diff body (with +/-/space prefixes)


class CodeAnchor(BaseModel):
    """A pointer into the post-change ('new') side of a file."""

    file: str
    line_range: tuple[int, int]  # inclusive (start, end)

    @property
    def is_single_line(self) -> bool:
        return self.line_range[0] == self.line_range[1]


class TourChunk(BaseModel):
    """One unit of the guided tour. Stable across the session."""

    chunk_id: str  # e.g. "c1", "c2"; assigned by the planner
    files: list[str]
    hunks: list[Hunk]
    summary: str = Field(..., description="One-line shown in the chunk list")
    rationale_for_position: str = Field(
        ...,
        description="Why this chunk appears at this position in the tour",
    )
    est_concern_level: Severity
    group: str | None = Field(
        None,
        description=(
            "Optional short label grouping this chunk with neighbours that "
            "serve the same narrative purpose (e.g. 'API surface', "
            "'Mechanism', 'Tests'). Rendered as a divider in the sidebar."
        ),
    )


class TourPlan(BaseModel):
    """The ordered tour for a single review session."""

    session_id: str
    pr: PRMetadata
    chunks: list[TourChunk]
    familiarity: FamiliarityLevel = Field(
        "review",
        description=(
            "How familiar the reviewer is with the change/repo. Controls "
            "narration depth — see FamiliarityLevel docstring. Default "
            "'review' assumes the reviewer knows the repo + language but "
            "not this specific PR."
        ),
    )
    multi_level: bool = Field(
        False,
        description=(
            "When True the backend generates narration at all four "
            "familiarity levels (parallel LLM calls) and the player UI "
            "shows a level switcher so the reviewer can A/B them live. "
            "`familiarity` then names the *initially active* level."
        ),
    )


class RelatedCode(BaseModel):
    """Code outside the diff that helps understand the change."""

    anchor: CodeAnchor
    relationship: RelationshipKind
    snippet: str  # already extracted by the backend; UI just displays
    # The specific line the retriever pointed at (the LSP hit itself,
    # not the surrounding context). The UI paints this row with a
    # subtle backlight so the reviewer can see which line was matched
    # without losing the surrounding context. 1-indexed, inside anchor
    # range. Defaults to anchor.line_range[0] for backward-compat.
    target_line: int | None = None


class Concern(BaseModel):
    """Something the model thinks deserves attention or a question."""

    severity: Severity
    text: str
    suggested_question: str = Field(
        ..., description="Ready-to-post wording for a PR comment"
    )
    anchor: CodeAnchor | None = None


class NarrationSegment(BaseModel):
    """One spoken segment of a chunk's narration.

    Anchored segments tell the UI to highlight + scroll those lines while
    the segment is being spoken. Unanchored segments are general commentary
    (intros, transitions, big-picture observations) — the diff stays put.
    """

    text: str = Field(
        ..., description="A few sentences. Sized for a single visual focus."
    )
    anchor: CodeAnchor | None = Field(
        None,
        description=(
            "Lines in the chunk's diff that this segment is talking about. "
            "Omit for general commentary."
        ),
    )


class ChunkNarration(BaseModel):
    """Everything generated for one chunk: script + side-panel data."""

    chunk_id: str
    narration: str = Field(
        ...,
        description=(
            "The full spoken script (intro + body, joined). Plain prose; "
            "TTS-friendly. This is just the concatenation of segments for "
            "transcript/display."
        ),
    )
    intro: str | None = Field(
        default=None,
        description=(
            "Optional whole-file / big-picture orientation that doesn't map to "
            "specific lines (e.g. 'this is the entry point for the auth flow'). "
            "Played first; surfaced in segments as a single unanchored segment "
            "at index 0. At most one per chunk."
        ),
    )
    segments: list[NarrationSegment] = Field(
        default_factory=list,
        description=(
            "Ordered narration segments. The first segment is the intro (if any) "
            "and carries no anchor; every subsequent segment is body and anchors "
            "to specific diff lines. The player drives diff highlight + scroll "
            "from each segment's anchor as the audio plays."
        ),
    )
    segment_offsets_ms: list[int] = Field(
        default_factory=list,
        description=(
            "Populated by the backend after TTS: cumulative start time in "
            "milliseconds for each segment in the concatenated audio. Same "
            "length as `segments`."
        ),
    )
    related_code: list[RelatedCode] = []
    concerns: list[Concern] = []


class FollowUp(BaseModel):
    """A user question raised mid-tour."""

    chunk_id: str | None = Field(
        None,
        description="Chunk the user was on when they asked; None if tour over",
    )
    question_text: str  # transcribed if voice
    transcript_confidence: float | None = None


class FollowUpAnswer(BaseModel):
    """LLM reply to a follow-up."""

    answer_text: str
    new_concerns: list[Concern] = []
    references: list[CodeAnchor] = []


class Flag(BaseModel):
    """A question/concern the reviewer wants tracked for the PR."""

    flag_id: str
    chunk_id: str
    anchor: CodeAnchor | None = None
    severity: Severity
    body: str = Field(..., description="Editable draft PR comment")
    posted: bool = False
    posted_url: str | None = None


class SessionState(BaseModel):
    """Snapshot returned by GET /sessions/{sid}."""

    plan: TourPlan
    current_chunk_id: str | None = None
    flags: list[Flag] = []
