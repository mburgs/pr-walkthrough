import { useState, useRef, useEffect } from "react";
import type { Concern, FollowUpAnswer } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import styles from "./FollowUpInput.module.css";

/**
 * Multi-turn follow-up chat panel.
 *
 * Layout:
 *   ┌────────────────────────────────┐
 *   │ ▾ Chat (N)        [Clear]      │  panel header — collapse handle
 *   ├────────────────────────────────┤
 *   │ › question                     │
 *   │   Answer: streamed text…       │  ← scrollable, capped at 35vh
 *   │ › another question             │
 *   │   Answer: …                    │
 *   ├────────────────────────────────┤
 *   │ [input]  [Ask]  [🎙]           │  input row — always visible
 *   └────────────────────────────────┘
 *
 * Each turn streams in token-by-token over SSE; a per-turn typewriter
 * reveals the text at a steady ~90 cps so the streaming is visibly
 * progressive even when the backend dumps a chunk at once. Pill
 * indicators on the answer header:
 *   `thinking…`  awaiting first token
 *   `streaming`  tokens arriving from LLM
 *   `typing`     LLM done, typewriter still revealing
 * Once both conditions clear the pill disappears.
 *
 * Concerns surfaced inside an answer get a `+ Flag` button that pushes
 * them into the session's flag tracker — same shape as the side-rail
 * Concerns section, so the user can capture promising threads found
 * mid-conversation without copy-pasting.
 */

type Phase = "transcribing" | "awaiting" | "streaming" | "complete" | "error";

interface Turn {
  id: string;
  question: string;
  isVoice: boolean;
  phase: Phase;
  streamingText: string;
  answer: FollowUpAnswer | null;
  audioUrl: string | null;
  answerId: string | null;
  error: string | null;
}

const REVEAL_CHARS_PER_SEC = 90;
const PANEL_COLLAPSED_KEY = "pr-walkthrough.followupCollapsed";

function loadBool(key: string): boolean {
  try { return localStorage.getItem(key) === "1"; } catch { return false; }
}

/** Reveals `target` one char at a time at a fixed rate. When the target
 * shrinks (e.g. new turn) the displayed text resets. When the source
 * stream completes (`finalized=true`), the remaining buffered text is
 * flushed instantly to avoid a long tail of typing. */
function useTypewriter(target: string, finalized: boolean): string {
  const [displayed, setDisplayed] = useState("");

  useEffect(() => {
    if (target.length < displayed.length) setDisplayed("");
  }, [target, displayed.length]);

  useEffect(() => {
    if (displayed.length >= target.length) return;
    if (finalized && target.length - displayed.length > 60) {
      setDisplayed(target);
      return;
    }
    const id = window.setTimeout(() => {
      const gap = target.length - displayed.length;
      const step = Math.max(1, Math.floor(gap / 8));
      setDisplayed(target.slice(0, displayed.length + step));
    }, 1000 / REVEAL_CHARS_PER_SEC);
    return () => window.clearTimeout(id);
  }, [target, displayed, finalized]);

  return displayed;
}

function FlagFromConcernButton({ concern, chunkId }: { concern: Concern; chunkId: string | null }) {
  const { addFlag } = useSession();
  const [added, setAdded] = useState(false);
  const [pending, setPending] = useState(false);

  if (!chunkId) return null;  // no anchor chunk → no place to attach

  const handleAdd = async () => {
    setPending(true);
    try {
      await addFlag({
        chunk_id: chunkId,
        anchor: concern.anchor,
        severity: concern.severity,
        body: concern.suggested_question || concern.text,
      });
      setAdded(true);
    } finally {
      setPending(false);
    }
  };

  if (added) return <span className={styles.flagged}>✓ flagged</span>;
  return (
    <button
      className={styles.flagBtn}
      onClick={handleAdd}
      disabled={pending}
      title="Add to flags"
    >+ Flag</button>
  );
}

function TurnView({ turn }: { turn: Turn }) {
  const { currentChunkId } = useSession();
  const target =
    turn.phase === "complete" && turn.answer
      ? turn.answer.answer_text
      : turn.streamingText;
  const finalized = turn.phase === "complete" || turn.phase === "error";
  const displayed = useTypewriter(target, finalized);

  const isInFlight =
    turn.phase === "transcribing" || turn.phase === "awaiting" || turn.phase === "streaming";
  const stillRevealing = displayed.length < target.length;

  // For voice turns, the question text only fills in after STT returns
  // (event: question). Show a placeholder while we're still
  // transcribing so the user knows what's happening.
  const renderQuestion = () => {
    if (turn.isVoice && turn.phase === "transcribing" && !turn.question) {
      return (
        <span className={styles.questionPending}>
          <span className={styles.spinner} aria-hidden /> transcribing audio…
        </span>
      );
    }
    return (
      <span className={styles.questionText}>
        {turn.question || (turn.isVoice ? "(no speech detected)" : "")}
      </span>
    );
  };

  return (
    <div className={styles.turn}>
      <div className={styles.question}>
        <span className={styles.questionLabel}>{turn.isVoice ? "🎙" : "›"}</span>
        {renderQuestion()}
      </div>
      <div className={styles.answer}>
        <div className={styles.answerHeader}>
          <span className={styles.answerLabel}>Answer</span>
          {turn.phase === "transcribing" && (
            <span className={styles.answerStatus}>
              <span className={styles.spinner} aria-hidden /> waiting on transcript
            </span>
          )}
          {turn.phase === "awaiting" && (
            <span className={styles.answerStatus}>
              <span className={styles.spinner} aria-hidden /> thinking…
            </span>
          )}
          {turn.phase === "streaming" && (
            <span className={styles.answerStatus}>
              <span className={styles.pulseDot} aria-hidden /> streaming
            </span>
          )}
          {turn.phase === "complete" && stillRevealing && (
            <span className={styles.answerStatus}>
              <span className={styles.pulseDot} aria-hidden /> typing
            </span>
          )}
        </div>
        {turn.phase === "awaiting" && !displayed ? (
          <div className={styles.answerTextPlaceholder}>
            Waiting for the first token…
          </div>
        ) : (
          <div className={styles.answerText}>
            {displayed}
            {(isInFlight || stillRevealing) && (
              <span className={styles.streamCaret} aria-hidden>▍</span>
            )}
          </div>
        )}
        {turn.error && (
          <div className={styles.answerError}>
            Stream error: {turn.error}
          </div>
        )}
        {turn.answer && turn.answer.new_concerns.length > 0 && (
          <div className={styles.newConcerns}>
            {turn.answer.new_concerns.map((c, i) => (
              <div key={i} className={styles.newConcern}>
                <div className={styles.newConcernRow}>
                  <span className={styles.newConcernSev}>[{c.severity}]</span>
                  <span className={styles.newConcernText}>{c.text}</span>
                </div>
                <FlagFromConcernButton concern={c} chunkId={currentChunkId} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default function FollowUpInput() {
  const { session, submitFollowUp } = useSession();
  const [text, setText] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [collapsed, setCollapsed] = useState<boolean>(() => loadBool(PANEL_COLLAPSED_KEY));
  const [recording, setRecording] = useState(false);
  const [micAvailable, setMicAvailable] = useState<boolean | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    localStorage.setItem(PANEL_COLLAPSED_KEY, collapsed ? "1" : "0");
  }, [collapsed]);

  useEffect(() => {
    navigator.mediaDevices?.getUserMedia({ audio: true })
      .then((s) => { s.getTracks().forEach((t) => t.stop()); setMicAvailable(true); })
      .catch(() => setMicAvailable(false));
  }, []);

  // Auto-scroll the log to the latest turn whenever its streamingText
  // grows or its phase advances. Skipped when collapsed (log isn't
  // rendered).
  useEffect(() => {
    if (collapsed || !logRef.current) return;
    logRef.current.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [collapsed, turns.length, turns[turns.length - 1]?.streamingText, turns[turns.length - 1]?.phase]);

  const inFlight = turns.some(
    (t) => t.phase === "transcribing" || t.phase === "awaiting" || t.phase === "streaming",
  );

  const updateTurn = (id: string, patch: Partial<Turn>) => {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  };
  const appendTurn = (turn: Turn) => setTurns((prev) => [...prev, turn]);

  const runStream = async (question: string, blob: Blob | undefined) => {
    const id = (typeof crypto !== "undefined" && "randomUUID" in crypto)
      ? crypto.randomUUID()
      : `t_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    // Asking a question auto-expands the panel so the user sees the
    // streaming response. They can collapse again afterward.
    if (collapsed) setCollapsed(false);
    appendTurn({
      id, question, isVoice: !!blob,
      // Voice submissions start in "transcribing" — the server will
      // emit `event: transcribing` immediately, but we pre-seed the
      // phase so the UI doesn't flicker "thinking" first.
      phase: blob ? "transcribing" : "awaiting",
      streamingText: "",
      answer: null, audioUrl: null, answerId: null, error: null,
    });

    try {
      const result = await submitFollowUp(question, blob, {
        onTranscribing: () => {
          updateTurn(id, { phase: "transcribing" });
        },
        onTranscribed: (text) => {
          // Fill in the question with what the model heard + move on
          // to waiting for the LLM. Empty text means STT found no
          // speech — render the placeholder rather than blank.
          updateTurn(id, { question: text, phase: "awaiting" });
        },
        onToken: (delta) => {
          setTurns((prev) => prev.map((t) =>
            t.id === id
              ? { ...t, streamingText: t.streamingText + delta, phase: "streaming" }
              : t
          ));
        },
      });
      updateTurn(id, {
        phase: "complete",
        answer: result.answer,
        audioUrl: result.audioUrl,
        answerId: result.answerId,
      });
    } catch (e) {
      updateTurn(id, { phase: "error", error: String(e) });
    }
  };

  const handleSubmit = async () => {
    const q = text.trim();
    if (!q || !session || inFlight) return;
    setText("");
    await runStream(q, undefined);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const toggleRecording = async () => {
    if (recording) {
      recorderRef.current?.stop();
      setRecording(false);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (e) => chunksRef.current.push(e.data);
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
        await runStream("", blob);
      };
      recorder.start();
      recorderRef.current = recorder;
      setRecording(true);
    } catch {
      setMicAvailable(false);
    }
  };

  const clearHistory = () => {
    if (inFlight) return;
    setTurns([]);
  };

  return (
    <div className={styles.bar}>
      {turns.length > 0 && (
        <div className={styles.panelHeader}>
          <button
            className={styles.panelToggle}
            onClick={() => setCollapsed((v) => !v)}
            title={collapsed ? "Show chat" : "Hide chat"}
          >
            <span className={styles.panelCaret} aria-hidden>{collapsed ? "▸" : "▾"}</span>
            Chat ({turns.length})
          </button>
          <button
            className={styles.clearBtn}
            onClick={clearHistory}
            disabled={inFlight}
            title="Clear conversation"
          >Clear</button>
        </div>
      )}
      {turns.length > 0 && !collapsed && (
        <div className={styles.log} ref={logRef}>
          {turns.map((t) => <TurnView key={t.id} turn={t} />)}
        </div>
      )}
      <div className={styles.row}>
        <input
          className={styles.input}
          type="text"
          placeholder={turns.length > 0
            ? "Ask another follow-up… (Enter to send)"
            : "Ask a follow-up question… (Enter to send)"}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={inFlight || !session}
        />
        <button
          className={styles.btn}
          onClick={handleSubmit}
          disabled={!text.trim() || inFlight || !session}
        >
          {inFlight ? (
            <><span className={styles.spinner} aria-hidden /> Asking…</>
          ) : "Ask"}
        </button>
        {micAvailable !== false && (
          <button
            className={`${styles.micBtn} ${recording ? styles.recording : ""}`}
            onClick={toggleRecording}
            disabled={inFlight || !session}
            title={recording ? "Stop recording" : "Push to talk"}
          >
            {recording ? "⏹" : "🎙"}
          </button>
        )}
      </div>
    </div>
  );
}
