/**
 * TypeScript types hand-translated from contracts/schemas.py and contracts/events.py.
 * Keep in sync manually whenever those Pydantic models change.
 * Single source of truth lives in contracts/ (Python); this file mirrors it.
 */

// ── Literals ─────────────────────────────────────────────────────────────────

export type Severity = "low" | "medium" | "high";
export type RelationshipKind =
  | "definition"
  | "callsite"
  | "test"
  | "prior_version"
  | "sibling";

// ── Core models ──────────────────────────────────────────────────────────────

export interface PRMetadata {
  url: string;
  repo: string; // "owner/name"
  number: number;
  title: string;
  author: string;
  base_ref: string;
  head_ref: string;
  base_sha: string;
  head_sha: string;
  body: string;
}

export interface Hunk {
  file: string;
  old_range: [number, number]; // (start_line, line_count); [0,0] for added files
  new_range: [number, number];
  header: string; // full @@ header line incl. function context
  body: string; // raw unified-diff body (with +/-/space prefixes)
}

export interface CodeAnchor {
  file: string;
  line_range: [number, number]; // inclusive (start, end)
}

export interface TourChunk {
  chunk_id: string;
  files: string[];
  hunks: Hunk[];
  summary: string;
  rationale_for_position: string;
  est_concern_level: Severity;
}

export interface TourPlan {
  session_id: string;
  pr: PRMetadata;
  chunks: TourChunk[];
}

export interface Highlight {
  anchor: CodeAnchor;
  why: string;
}

export interface RelatedCode {
  anchor: CodeAnchor;
  relationship: RelationshipKind;
  snippet: string;
}

export interface Concern {
  severity: Severity;
  text: string;
  suggested_question: string;
  anchor: CodeAnchor | null;
}

export interface ChunkNarration {
  chunk_id: string;
  narration: string;
  highlights: Highlight[];
  related_code: RelatedCode[];
  concerns: Concern[];
  look_closer_for: string[];
}

export interface FollowUp {
  chunk_id: string | null;
  question_text: string;
  transcript_confidence: number | null;
}

export interface FollowUpAnswer {
  answer_text: string;
  new_concerns: Concern[];
  references: CodeAnchor[];
}

export interface Flag {
  flag_id: string;
  chunk_id: string;
  anchor: CodeAnchor | null;
  severity: Severity;
  body: string;
  posted: boolean;
  posted_url: string | null;
}

export interface SessionState {
  plan: TourPlan;
  current_chunk_id: string | null;
  flags: Flag[];
}

// ── SSE event types (mirrors contracts/events.py) ────────────────────────────

export interface ChunkStartedEvent {
  event_type: "chunk_started";
  chunk_id: string;
}

export interface NarrationTokenEvent {
  event_type: "narration_token";
  chunk_id: string;
  text: string;
}

export interface ChunkCompleteEvent {
  event_type: "chunk_complete";
  chunk_id: string;
}

export interface AudioReadyEvent {
  event_type: "audio_ready";
  chunk_id: string;
  url: string;
}

export interface FlagSuggestedEvent {
  event_type: "flag_suggested";
  chunk_id: string;
  concern: Concern;
}

export interface ErrorEvent {
  event_type: "error";
  message: string;
  recoverable: boolean;
}

export type SSEEvent =
  | ChunkStartedEvent
  | NarrationTokenEvent
  | ChunkCompleteEvent
  | AudioReadyEvent
  | FlagSuggestedEvent
  | ErrorEvent;
