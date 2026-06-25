/**
 * API client. All calls go through VITE_BACKEND_URL (default: "" = same origin).
 * In development, MSW intercepts requests before they hit the network.
 */

import type {
  TourPlan,
  SessionState,
  ChunkNarration,
  Flag,
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

export function createSession(prUrl: string): Promise<TourPlan> {
  return request<TourPlan>("/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pr_url: prUrl }),
  });
}

export function getSession(sid: string): Promise<SessionState> {
  return request<SessionState>(`/sessions/${sid}`);
}

export function getChunkNarration(sid: string, cid: string): Promise<ChunkNarration> {
  return request<ChunkNarration>(`/sessions/${sid}/chunks/${cid}`);
}

export function regenerateChunk(sid: string, cid: string): Promise<{ status: string; chunk_id: string }> {
  return request(`/sessions/${sid}/chunks/${cid}/regenerate`, { method: "POST" });
}

export function getAudioUrl(sid: string, cid: string): string {
  return `${BASE}/sessions/${sid}/chunks/${cid}/audio`;
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

export async function submitFollowUp(
  sid: string,
  chunkId: string | null,
  text: string,
  audioBlob?: Blob
): Promise<FollowUpAnswer> {
  if (audioBlob) {
    return request<FollowUpAnswer>(`/sessions/${sid}/follow-up`, {
      method: "POST",
      headers: { "Content-Type": audioBlob.type || "audio/webm" },
      body: audioBlob,
    });
  }
  return request<FollowUpAnswer>(`/sessions/${sid}/follow-up`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chunk_id: chunkId,
      question_text: text,
      transcript_confidence: null,
    }),
  });
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
