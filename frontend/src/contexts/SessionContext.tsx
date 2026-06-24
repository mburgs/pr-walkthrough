import { createContext, useContext, useState, useCallback, useEffect } from "react";
import type { SessionState, Flag, ChunkNarration, FollowUpAnswer } from "../contracts";
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
  initSession: (prUrl: string) => Promise<void>;
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

  // Load narration whenever chunk changes
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
  }, [session, currentChunkId]);

  const initSession = useCallback(async (prUrl: string) => {
    setLoading(true);
    setError(null);
    try {
      const plan = await api.createSession(prUrl);
      const state = await api.getSession(plan.session_id);
      setSession(state);
      setFlags(state.flags);
      if (plan.chunks.length > 0) {
        setCurrentChunkId(plan.chunks[0].chunk_id);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

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
