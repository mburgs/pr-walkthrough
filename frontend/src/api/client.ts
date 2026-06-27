/**
 * API client. All calls go through VITE_BACKEND_URL (default: "" = same origin).
 * In development, MSW intercepts requests before they hit the network.
 */

import type {
  TourPlan,
  SessionState,
  ChunkNarration,
  Flag,
  FamiliarityLevel,
  FollowUpAnswer,
} from "../contracts";

const BASE = (import.meta.env.VITE_BACKEND_URL as string | undefined) ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${body}`);
  }
  // 204 No Content (e.g. DELETE /flags) has an empty body — json() throws.
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export function createSession(
  prUrl: string,
  familiarity: FamiliarityLevel = "review",
  multiLevel: boolean = false,
): Promise<TourPlan> {
  return request<TourPlan>("/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pr_url: prUrl, familiarity, multi_level: multiLevel }),
  });
}

export function getSession(sid: string): Promise<SessionState> {
  return request<SessionState>(`/sessions/${sid}`);
}

export function getChunkNarration(
  sid: string,
  cid: string,
  level?: FamiliarityLevel,
): Promise<ChunkNarration> {
  const q = level ? `?level=${encodeURIComponent(level)}` : "";
  return request<ChunkNarration>(`/sessions/${sid}/chunks/${cid}${q}`);
}

export function regenerateChunk(sid: string, cid: string): Promise<{ status: string; chunk_id: string }> {
  return request(`/sessions/${sid}/chunks/${cid}/regenerate`, { method: "POST" });
}

export function getRepoFile(sid: string, path: string): Promise<{ path: string; content: string }> {
  return request(`/sessions/${sid}/files?path=${encodeURIComponent(path)}`);
}

export function getAudioUrl(sid: string, cid: string, level?: FamiliarityLevel): string {
  const q = level ? `?level=${encodeURIComponent(level)}` : "";
  return `${BASE}/sessions/${sid}/chunks/${cid}/audio${q}`;
}

export function getVariantAudioUrl(
  sid: string, cid: string, engine: string, filtered: boolean
): string {
  return `${BASE}/sessions/${sid}/chunks/${cid}/audio.variant?engine=${encodeURIComponent(engine)}&filtered=${filtered}`;
}

export async function getAvailableEngines(sid: string, cid: string): Promise<{
  engines: string[];
  cached: { engine: string; filtered: boolean }[];
}> {
  return request(`/sessions/${sid}/chunks/${cid}/audio/variants`);
}

/**
 * Fetch a variant's offsets (from response header). Returns null on 504/error.
 * The audio itself is downloaded and turned into a blob URL so the player can
 * point its <audio> src at it once and replay without re-hitting the server.
 */
export async function fetchVariant(
  sid: string, cid: string, engine: string, filtered: boolean
): Promise<{ blobUrl: string; offsetsMs: number[] } | null> {
  const url = getVariantAudioUrl(sid, cid, engine, filtered);
  const res = await fetch(url);
  if (!res.ok) return null;
  const blob = await res.blob();
  const offsetsHeader = res.headers.get("X-Segment-Offsets-Ms");
  let offsetsMs: number[] = [];
  if (offsetsHeader) {
    try { offsetsMs = JSON.parse(offsetsHeader); } catch { /* ignore */ }
  }
  return { blobUrl: URL.createObjectURL(blob), offsetsMs };
}

export function getFollowUpAudioUrl(sid: string, aid: string): string {
  return `${BASE}/sessions/${sid}/follow-up/${aid}/audio`;
}

/** Result returned by the streaming follow-up call once the SSE stream
 * has been fully consumed: the final structured answer plus the URL
 * the audio for it lives at. */
export interface FollowUpStreamResult {
  answer: FollowUpAnswer;
  audioUrl: string;
  answerId: string;
}

/** Callbacks for {@link submitFollowUp}. All are optional. */
export interface FollowUpStreamCallbacks {
  /** Fired for each `event: token` — `text` is a delta to append. */
  onToken?: (text: string) => void;
  /** Fired once when the stream opens (server is alive). */
  onOpen?: () => void;
}

/**
 * Submit a follow-up question and consume the SSE response stream.
 *
 * The backend now streams the answer token-by-token via Server-Sent
 * Events, so the UI can show typing as it arrives. This client parses
 * the event stream manually (no `EventSource` because POST + binary
 * body aren't supported by it) and resolves with the final structured
 * payload once `event: final` is seen.
 */
export async function submitFollowUp(
  sid: string,
  chunkId: string | null,
  text: string,
  audioBlob: Blob | undefined,
  callbacks: FollowUpStreamCallbacks = {},
): Promise<FollowUpStreamResult> {
  const init: RequestInit = audioBlob
    ? {
        method: "POST",
        headers: { "Content-Type": audioBlob.type || "audio/webm", Accept: "text/event-stream" },
        body: audioBlob,
      }
    : {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({
          chunk_id: chunkId,
          question_text: text,
          transcript_confidence: null,
        }),
      };

  const resp = await fetch(`${BASE}/sessions/${sid}/follow-up`, init);
  if (!resp.ok || !resp.body) {
    throw new Error(`POST /follow-up ${resp.status}: ${await resp.text().catch(() => "")}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: FollowUpStreamResult | null = null;

  // SSE frames are separated by blank lines. We accumulate into `buffer`
  // and flush whenever we see a "\n\n", processing each frame.
  while (true) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    if (done) break;

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const parsed = parseSseFrame(frame);
      if (!parsed) continue;
      const { event, data } = parsed;
      if (event === "open") {
        callbacks.onOpen?.();
      } else if (event === "token") {
        try {
          const payload = JSON.parse(data) as { text?: string };
          if (payload.text) callbacks.onToken?.(payload.text);
        } catch {
          /* malformed frame — skip rather than abort the whole stream */
        }
      } else if (event === "final") {
        const payload = JSON.parse(data) as {
          answer: FollowUpAnswer;
          audio_url: string;
          answer_id: string;
        };
        finalResult = {
          answer: payload.answer,
          audioUrl: payload.audio_url,
          answerId: payload.answer_id,
        };
      } else if (event === "error") {
        const payload = JSON.parse(data) as { message?: string };
        throw new Error(payload.message ?? "Follow-up stream errored");
      }
    }
  }

  if (!finalResult) {
    throw new Error("Follow-up stream ended without a `final` event");
  }
  return finalResult;
}

function parseSseFrame(frame: string): { event: string; data: string } | null {
  let event = "message";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data = line.slice(5).trim();
  }
  if (!data) return null;
  return { event, data };
}

export function createFlag(
  sid: string,
  flag: Omit<Flag, "flag_id" | "posted" | "posted_url">
): Promise<Flag> {
  return request<Flag>(`/sessions/${sid}/flags`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(flag),
  });
}

export function patchFlag(sid: string, fid: string, partial: Partial<Flag>): Promise<Flag> {
  return request<Flag>(`/sessions/${sid}/flags/${fid}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(partial),
  });
}

export function postFlag(sid: string, fid: string): Promise<Flag> {
  return request<Flag>(`/sessions/${sid}/flags/${fid}/post`, {
    method: "POST",
  });
}

export function deleteFlag(sid: string, fid: string): Promise<void> {
  return request<void>(`/sessions/${sid}/flags/${fid}`, { method: "DELETE" });
}
