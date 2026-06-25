/**
 * MSW request handlers — mock backend for development.
 * Serves fixture data from fixtures/pr_small/ for all API endpoints.
 */

import { http, HttpResponse } from "msw";
import tourPlan from "./fixtures/tour_plan.json";
import c1 from "./fixtures/c1.narration.json";
import c2 from "./fixtures/c2.narration.json";
import c3 from "./fixtures/c3.narration.json";
import followUpExample from "./fixtures/follow_up_example.json";
import flagsExample from "./fixtures/flags_example.json";
import type { Flag, SessionState, TourPlan } from "../contracts";

const SESSION_ID = "sess_pr_small_001";
const plan = tourPlan as TourPlan;

// In-memory store for flags (supports create/patch/delete during the session)
let flags: Flag[] = (flagsExample as Flag[]).map((f) => ({ ...f }));
let flagCounter = 100;

const narrations: Record<string, unknown> = { c1, c2, c3 };

export const handlers = [
  // POST /sessions — create a new session
  http.post("/sessions", async () => {
    return HttpResponse.json(plan);
  }),

  // GET /sessions/:sid — session state snapshot
  http.get("/sessions/:sid", ({ params }) => {
    if (params.sid !== SESSION_ID) {
      return HttpResponse.json({ detail: "session not found" }, { status: 404 });
    }
    const state: SessionState = {
      plan,
      current_chunk_id: plan.chunks[0]?.chunk_id ?? null,
      flags: [...flags],
    };
    return HttpResponse.json(state);
  }),

  // GET /sessions/:sid/chunks/:cid — chunk narration
  http.get("/sessions/:sid/chunks/:cid", ({ params }) => {
    const narration = narrations[params.cid as string];
    if (!narration) {
      return HttpResponse.json({ detail: "chunk not found" }, { status: 404 });
    }
    return HttpResponse.json(narration);
  }),

  // GET /sessions/:sid/chunks/:cid/audio — serve silent WAV
  http.get("/sessions/:sid/chunks/:cid/audio", () => {
    // Redirect to the static silent.wav in public/
    return HttpResponse.redirect("/silent.wav");
  }),

  // GET /sessions/:sid/chunks/:cid/audio/variants — engine list + cached combos
  http.get("/sessions/:sid/chunks/:cid/audio/variants", () => {
    return HttpResponse.json({ engines: ["kokoro"], cached: [{ engine: "kokoro", filtered: true }] });
  }),

  // GET /sessions/:sid/chunks/:cid/audio.variant — single variant audio
  http.get("/sessions/:sid/chunks/:cid/audio.variant", async () => {
    const r = await fetch("/silent.wav");
    const blob = await r.blob();
    return new HttpResponse(blob, {
      headers: {
        "Content-Type": "audio/wav",
        "X-Segment-Offsets-Ms": JSON.stringify([0, 50]),
        "Access-Control-Expose-Headers": "X-Segment-Offsets-Ms",
      },
    });
  }),

  // POST /sessions/:sid/chunks/:cid/regenerate — wipe + re-kick (MSW just acks)
  http.post("/sessions/:sid/chunks/:cid/regenerate", ({ params }) => {
    return HttpResponse.json({ status: "regenerating", chunk_id: params.cid });
  }),

  // GET /sessions/:sid/files?path= — full file contents for related-code modal
  http.get("/sessions/:sid/files", ({ request }) => {
    const url = new URL(request.url);
    const path = url.searchParams.get("path") ?? "(unknown)";
    // Stub: return a multi-line shaped sample so the modal renders meaningfully
    // against fixture related-code anchors.
    const content = Array.from({ length: 60 }, (_, i) =>
      i === 11 ? `def example_${i + 1}():  # ← target` : `def example_${i + 1}():`
    ).join("\n");
    return HttpResponse.json({ path, content });
  }),

  // GET /sessions/:sid/follow-up/:aid/audio — answer audio (also silent)
  http.get("/sessions/:sid/follow-up/:aid/audio", () => {
    return HttpResponse.redirect("/silent.wav");
  }),

  // POST /sessions/:sid/follow-up — text or audio follow-up
  http.post("/sessions/:sid/follow-up", async () => {
    // Always return the fixture answer
    return HttpResponse.json(followUpExample.answer, {
      headers: {
        "X-Answer-Audio-Url": `/sessions/${SESSION_ID}/follow-up/ans_001/audio`,
      },
    });
  }),

  // POST /sessions/:sid/flags — create a flag
  http.post("/sessions/:sid/flags", async ({ request }) => {
    const body = (await request.json()) as Omit<Flag, "flag_id" | "posted" | "posted_url">;
    const newFlag: Flag = {
      ...body,
      flag_id: `flag_${++flagCounter}`,
      posted: false,
      posted_url: null,
    };
    flags = [...flags, newFlag];
    return HttpResponse.json(newFlag, { status: 201 });
  }),

  // PATCH /sessions/:sid/flags/:fid — edit a flag
  http.patch("/sessions/:sid/flags/:fid", async ({ params, request }) => {
    const fid = params.fid as string;
    const patch = (await request.json()) as Partial<Flag>;
    const idx = flags.findIndex((f) => f.flag_id === fid);
    if (idx === -1) {
      return HttpResponse.json({ detail: "flag not found" }, { status: 404 });
    }
    flags[idx] = { ...flags[idx], ...patch };
    return HttpResponse.json(flags[idx]);
  }),

  // POST /sessions/:sid/flags/:fid/post — post flag to PR
  http.post("/sessions/:sid/flags/:fid/post", ({ params }) => {
    const fid = params.fid as string;
    const idx = flags.findIndex((f) => f.flag_id === fid);
    if (idx === -1) {
      return HttpResponse.json({ detail: "flag not found" }, { status: 404 });
    }
    flags[idx] = {
      ...flags[idx],
      posted: true,
      posted_url: `https://github.com/example-org/auth-service/pull/142#issuecomment-mock-${fid}`,
    };
    return HttpResponse.json(flags[idx]);
  }),

  // DELETE /sessions/:sid/flags/:fid — remove a flag
  http.delete("/sessions/:sid/flags/:fid", ({ params }) => {
    const fid = params.fid as string;
    flags = flags.filter((f) => f.flag_id !== fid);
    return new HttpResponse(null, { status: 204 });
  }),

  // GET /sessions/:sid/events — SSE stub (sends a few events then closes)
  http.get("/sessions/:sid/events", () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        const events = [
          `event: chunk_started\ndata: ${JSON.stringify({ chunk_id: "c1", event_type: "chunk_started" })}\n\n`,
          `event: chunk_complete\ndata: ${JSON.stringify({ chunk_id: "c1", event_type: "chunk_complete" })}\n\n`,
          `event: audio_ready\ndata: ${JSON.stringify({ chunk_id: "c1", url: "/silent.wav", event_type: "audio_ready" })}\n\n`,
        ];
        for (const e of events) {
          controller.enqueue(encoder.encode(e));
        }
        controller.close();
      },
    });
    return new HttpResponse(stream, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
      },
    });
  }),
];
