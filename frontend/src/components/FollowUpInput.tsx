import { useState, useRef, useEffect } from "react";
import type { FollowUpAnswer } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import styles from "./FollowUpInput.module.css";

/**
 * Multi-turn follow-up chat.
 *
 * Each Ask appends a `Turn` to the history and is never dropped — the
 * user can scroll back to see prior Q&A. The active turn streams in
 * token-by-token over the SSE backend, but the rendered text is driven
 * by a per-turn typewriter buffer that reveals at a steady ~80 cps so
 * the streaming is *visibly* progressive even when the network/LLM
 * dumps a chunk all at once.
 *
 * Per-turn collapsible answer; questions stay visible above their
 * answers as static labels.
 */

type Phase = "awaiting" | "streaming" | "complete" | "error";

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
  collapsed: boolean;
}

const REVEAL_CHARS_PER_SEC = 90;

/** Reveals `target` one char at a time at a fixed rate. When the target
 * shrinks (e.g. new turn) the displayed text resets. When the source
 * stream completes (`finalized=true`), the remaining buffered text is
 * flushed instantly to avoid a long tail of typing. */
function useTypewriter(target: string, finalized: boolean): string {
  const [displayed, setDisplayed] = useState("");

  // Reset displayed when target shrinks (a fresh turn starts at "").
  useEffect(() => {
    if (target.length < displayed.length) setDisplayed("");
  }, [target, displayed.length]);

  // Tick: catch up one char at a time. If finalized + we're behind by a
  // lot, flush to the end so the user isn't stuck watching a slow type.
  useEffect(() => {
    if (displayed.length >= target.length) return;
    if (finalized && target.length - displayed.length > 60) {
      setDisplayed(target);
      return;
    }
    const id = window.setTimeout(() => {
      // Reveal a chunk proportional to how far behind we are, so a
      // burst of network activity catches up faster than 1-char-at-a-
      // time. Floor at 1 so progress always happens.
      const gap = target.length - displayed.length;
      const step = Math.max(1, Math.floor(gap / 8));
      setDisplayed(target.slice(0, displayed.length + step));
    }, 1000 / REVEAL_CHARS_PER_SEC);
    return () => window.clearTimeout(id);
  }, [target, displayed, finalized]);

  return displayed;
}

function TurnView({ turn, onToggle }: { turn: Turn; onToggle: () => void }) {
  // The target the typewriter chases. Once complete, switch to the
  // final answer's text (already equal to streamingText if streaming
  // worked, but the structured answer is authoritative).
  const target =
    turn.phase === "complete" && turn.answer
      ? turn.answer.answer_text
      : turn.streamingText;
  const finalized = turn.phase === "complete" || turn.phase === "error";
  const displayed = useTypewriter(target, finalized);

  const isInFlight = turn.phase === "awaiting" || turn.phase === "streaming";
  const stillRevealing = displayed.length < target.length;

  return (
    <div className={styles.turn}>
      <div className={styles.question}>
        <span className={styles.questionLabel}>{turn.isVoice ? "🎙" : "›"}</span>
        <span className={styles.questionText}>{turn.question || (turn.isVoice ? "(voice)" : "")}</span>
      </div>
      <div className={styles.answer}>
        <button
          className={styles.answerHeader}
          onClick={onToggle}
          disabled={isInFlight}
          title={isInFlight ? undefined : (turn.collapsed ? "Expand answer" : "Collapse answer")}
        >
          <span className={styles.answerCaret} aria-hidden>
            {turn.collapsed ? "▸" : "▾"}
          </span>
          <span className={styles.answerLabel}>Answer</span>
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
        </button>
        {!turn.collapsed && (
          <>
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
                    [{c.severity}] {c.text}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default function FollowUpInput() {
  const { session, submitFollowUp } = useSession();
  const [text, setText] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [recording, setRecording] = useState(false);
  const [micAvailable, setMicAvailable] = useState<boolean | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const logRef = useRef<HTMLDivElement | null>(null);

  // Probe mic availability once
  useEffect(() => {
    navigator.mediaDevices?.getUserMedia({ audio: true })
      .then((s) => { s.getTracks().forEach((t) => t.stop()); setMicAvailable(true); })
      .catch(() => setMicAvailable(false));
  }, []);

  // Auto-scroll the log to the bottom whenever a new turn arrives or
  // the active turn keeps streaming. Smooth scroll so the user can see
  // direction-of-motion when many turns are stacked.
  useEffect(() => {
    if (!logRef.current) return;
    logRef.current.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [turns.length, turns[turns.length - 1]?.streamingText, turns[turns.length - 1]?.phase]);

  const inFlight = turns.some((t) => t.phase === "awaiting" || t.phase === "streaming");

  const updateTurn = (id: string, patch: Partial<Turn>) => {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  };

  const appendTurn = (turn: Turn) => setTurns((prev) => [...prev, turn]);

  const runStream = async (question: string, blob: Blob | undefined) => {
    const id = (typeof crypto !== "undefined" && "randomUUID" in crypto)
      ? crypto.randomUUID()
      : `t_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    appendTurn({
      id,
      question,
      isVoice: !!blob,
      phase: "awaiting",
      streamingText: "",
      answer: null,
      audioUrl: null,
      answerId: null,
      error: null,
      collapsed: false,
    });

    try {
      const result = await submitFollowUp(question, blob, {
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
        // Question text is filled in once STT returns; until then show a placeholder
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
        <div className={styles.log} ref={logRef}>
          {turns.map((t) => (
            <TurnView
              key={t.id}
              turn={t}
              onToggle={() =>
                updateTurn(t.id, { collapsed: !t.collapsed })
              }
            />
          ))}
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
        {turns.length > 0 && (
          <button
            className={styles.clearBtn}
            onClick={clearHistory}
            disabled={inFlight}
            title="Clear conversation"
          >Clear</button>
        )}
      </div>
    </div>
  );
}
