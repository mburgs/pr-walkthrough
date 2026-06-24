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

export function getAudioUrl(sid: string, cid: string): string {
  return `${BASE}/sessions/${sid}/chunks/${cid}/audio`;
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
