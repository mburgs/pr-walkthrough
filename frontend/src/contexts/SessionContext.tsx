import { createContext, useContext, useState, useCallback, useEffect } from "react";
import type { SessionState, Flag, ChunkNarration, FamiliarityLevel, FollowUpAnswer } from "../contracts";
import * as api from "../api/client";

interface SessionContextValue {
  session: SessionState | null;
  loading: boolean;
  error: string | null;
  currentChunkId: string | null;
  currentNarration: ChunkNarration | null;
  narrationLoading: boolean;
  flags: Flag[];
  setCurrentChunkId: (id: string) => void;
  addFlag: (flag: Omit<Flag, "flag_id" | "posted" | "posted_url">) => Promise<Flag>;
  updateFlag: (fid: string, partial: Partial<Flag>) => Promise<Flag>;
  postFlag: (fid: string) => Promise<Flag>;
  deleteFlag: (fid: string) => Promise<void>;
  submitFollowUp: (text: string, audioBlob?: Blob) => Promise<FollowUpAnswer>;
  initSession: (prUrl: string, familiarity?: FamiliarityLevel) => Promise<void>;
  resumeSession: (sid: string) => Promise<void>;
  /** Wipe the current chunk's narration + audio cache and re-fetch. Returns
   * a busting key callers can append to URLs (e.g. audio src) so the browser
   * doesn't reuse a stale resource. */
  regenerateCurrentChunk: () => Promise<void>;
  /** Per-chunk monotonic generation counter — appended to audio src so the
   * browser doesn't replay cached bytes from before a regenerate. Keyed by
   * chunk_id so regenerating c1 doesn't also bust c2's cached audio. */
  narrationGen: Record<string, number>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<SessionState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [currentChunkId, setCurrentChunkId] = useState<string | null>(null);
  const [currentNarration, setCurrentNarration] = useState<ChunkNarration | null>(null);
  const [narrationLoading, setNarrationLoading] = useState(false);
  const [flags, setFlags] = useState<Flag[]>([]);
  const [narrationGen, setNarrationGen] = useState<Record<string, number>>({});

  // Load narration whenever chunk changes, or this chunk's gen counter bumps.
  // Watching the whole `narrationGen` map would refetch every chunk on any
  // regenerate; pull the single per-chunk number into the dep list instead.
  const currentGen = currentChunkId ? (narrationGen[currentChunkId] ?? 0) : 0;
  useEffect(() => {
    if (!session || !currentChunkId) return;
    let cancelled = false;
    setNarrationLoading(true);
    setCurrentNarration(null);
    api
      .getChunkNarration(session.plan.session_id, currentChunkId)
      .then((narration) => {
        if (!cancelled) {
          setCurrentNarration(narration);
          setNarrationLoading(false);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(String(e));
          setNarrationLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [session, currentChunkId, currentGen]);

  // Reset all session-scoped state. Used by initSession + resumeSession so
  // we don't briefly show the previous session's narration/flags while the
  // new session loads (worst case: identical chunk ids would mask the swap
  // entirely since the chunk-id-keyed effect wouldn't fire).
  const resetSessionState = useCallback(() => {
    setSession(null);
    setCurrentChunkId(null);
    setCurrentNarration(null);
    setNarrationLoading(false);
    setFlags([]);
    setNarrationGen({});
  }, []);

  const initSession = useCallback(async (prUrl: string, familiarity: FamiliarityLevel = "review") => {
    resetSessionState();
    setLoading(true);
    setError(null);
    try {
      const plan = await api.createSession(prUrl, familiarity);
      const state = await api.getSession(plan.session_id);
      setSession(state);
      setFlags(state.flags);
      if (plan.chunks.length > 0) {
        setCurrentChunkId(plan.chunks[0].chunk_id);
      }
      // Persist sid in URL hash so reloads land back in this session (M7).
      // Also drop the one-shot `?pr=` query, otherwise the address bar shows
      // both — and if this session is ever evicted server-side, reloading
      // would silently create a *new* session from the stale `?pr=` value.
      const next = new URL(window.location.href);
      next.searchParams.delete("pr");
      next.hash = `session=${plan.session_id}`;
      window.history.replaceState({}, "", next.toString());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [resetSessionState]);

  const regenerateCurrentChunk = useCallback(async () => {
    if (!session || !currentChunkId) return;
    await api.regenerateChunk(session.plan.session_id, currentChunkId);
    setNarrationGen((prev) => ({
      ...prev,
      [currentChunkId]: (prev[currentChunkId] ?? 0) + 1,
    }));
  }, [session, currentChunkId]);

  const resumeSession = useCallback(async (sid: string) => {
    resetSessionState();
    setLoading(true);
    setError(null);
    try {
      const state = await api.getSession(sid);
      setSession(state);
      setFlags(state.flags);
      if (state.plan.chunks.length > 0) {
        setCurrentChunkId(state.plan.chunks[0].chunk_id);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [resetSessionState]);

  const addFlag = useCallback(
    async (flag: Omit<Flag, "flag_id" | "posted" | "posted_url">) => {
      if (!session) throw new Error("No session");
      const created = await api.createFlag(session.plan.session_id, flag);
      setFlags((prev) => [...prev, created]);
      return created;
    },
    [session]
  );

  const updateFlag = useCallback(
    async (fid: string, partial: Partial<Flag>) => {
      if (!session) throw new Error("No session");
      const updated = await api.patchFlag(session.plan.session_id, fid, partial);
      setFlags((prev) => prev.map((f) => (f.flag_id === fid ? updated : f)));
      return updated;
    },
    [session]
  );

  const postFlag = useCallback(
    async (fid: string) => {
      if (!session) throw new Error("No session");
      const posted = await api.postFlag(session.plan.session_id, fid);
      setFlags((prev) => prev.map((f) => (f.flag_id === fid ? posted : f)));
      return posted;
    },
    [session]
  );

  const deleteFlag = useCallback(
    async (fid: string) => {
      if (!session) throw new Error("No session");
      await api.deleteFlag(session.plan.session_id, fid);
      setFlags((prev) => prev.filter((f) => f.flag_id !== fid));
    },
    [session]
  );

  const submitFollowUp = useCallback(
    async (text: string, audioBlob?: Blob) => {
      if (!session) throw new Error("No session");
      return api.submitFollowUp(session.plan.session_id, currentChunkId, text, audioBlob);
    },
    [session, currentChunkId]
  );

  return (
    <SessionContext.Provider
      value={{
        session,
        loading,
        error,
        currentChunkId,
        currentNarration,
        narrationLoading,
        flags,
        setCurrentChunkId,
        addFlag,
        updateFlag,
        postFlag,
        deleteFlag,
        submitFollowUp,
        initSession,
        resumeSession,
        regenerateCurrentChunk,
        narrationGen,
      }}
    >
      {children}
    </SessionContext.Provider>
  );
}

export function useSession() {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used within SessionProvider");
  return ctx;
}
