import { useState, useRef, useEffect } from "react";
import type { FollowUpAnswer } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import styles from "./FollowUpInput.module.css";

/**
 * Streaming follow-up Q&A.
 *
 * The submit handler kicks an SSE stream and renders three phases:
 *   1. `awaiting`   — request sent, no tokens yet (spinner + label).
 *   2. `streaming`  — first token arrived; render `streamingText` live
 *                     with a cursor and a small "streaming…" indicator.
 *   3. `complete`   — `event: final` arrived. Render the structured
 *                     answer (text + new_concerns). User can collapse.
 */
export default function FollowUpInput() {
  const { session, submitFollowUp } = useSession();
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<"idle" | "awaiting" | "streaming" | "complete">("idle");
  const [streamingText, setStreamingText] = useState("");
  const [answer, setAnswer] = useState<FollowUpAnswer | null>(null);
  const [answerCollapsed, setAnswerCollapsed] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [micAvailable, setMicAvailable] = useState<boolean | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  // Probe mic availability once
  useEffect(() => {
    navigator.mediaDevices?.getUserMedia({ audio: true })
      .then((s) => { s.getTracks().forEach((t) => t.stop()); setMicAvailable(true); })
      .catch(() => setMicAvailable(false));
  }, []);

  const beginStream = () => {
    setPhase("awaiting");
    setStreamingText("");
    setAnswer(null);
    setAnswerCollapsed(false);
    setStreamError(null);
  };

  const runStream = async (q: string, blob?: Blob) => {
    beginStream();
    try {
      const result = await submitFollowUp(q, blob, {
        onOpen: () => {
          // Server is alive — UI already showing the spinner; nothing extra.
        },
        onToken: (delta) => {
          // Functional update + phase flip both inside the same setState
          // batch so React doesn't render the spinner-only frame after
          // the first token arrives.
          setStreamingText((prev) => prev + delta);
          setPhase((p) => (p === "awaiting" ? "streaming" : p));
        },
      });
      setAnswer(result.answer);
      setPhase("complete");
    } catch (e) {
      setStreamError(String(e));
      setPhase("complete");  // show whatever streamed before the error
    }
  };

  const handleSubmit = async () => {
    const q = text.trim();
    if (!q || !session || phase === "awaiting" || phase === "streaming") return;
    setText("");
    await runStream(q);
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

  const inFlight = phase === "awaiting" || phase === "streaming";
  // Show *something* in the answer area for every non-idle phase.
  const showAnswerArea = phase !== "idle";
  // Text to display: the streaming buffer while in-flight, the final
  // answer text otherwise. Streaming buffer is also shown if final
  // failed mid-stream and we have nothing better.
  const displayText = inFlight
    ? streamingText
    : (answer?.answer_text ?? streamingText);

  return (
    <div className={styles.bar}>
      <div className={styles.row}>
        <input
          className={styles.input}
          type="text"
          placeholder="Ask a follow-up question… (Enter to send)"
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

      {showAnswerArea && (
        <div className={styles.answer}>
          <button
            className={styles.answerHeader}
            onClick={() => setAnswerCollapsed((v) => !v)}
            disabled={phase !== "complete"}
            title={phase === "complete" ? (answerCollapsed ? "Expand answer" : "Collapse answer") : undefined}
          >
            <span className={styles.answerCaret} aria-hidden>
              {answerCollapsed ? "▸" : "▾"}
            </span>
            <span className={styles.answerLabel}>Answer</span>
            {phase === "awaiting" && (
              <span className={styles.answerStatus}>
                <span className={styles.spinner} aria-hidden /> thinking…
              </span>
            )}
            {phase === "streaming" && (
              <span className={styles.answerStatus}>
                <span className={styles.pulseDot} aria-hidden /> streaming
              </span>
            )}
          </button>
          {!answerCollapsed && (
            <>
              {phase === "awaiting" && !streamingText ? (
                <div className={styles.answerTextPlaceholder}>
                  Waiting for the first token…
                </div>
              ) : (
                <div className={styles.answerText}>
                  {displayText}
                  {inFlight && <span className={styles.streamCaret} aria-hidden>▍</span>}
                </div>
              )}
              {streamError && (
                <div className={styles.answerError}>
                  Stream error: {streamError}
                </div>
              )}
              {answer && answer.new_concerns.length > 0 && (
                <div className={styles.newConcerns}>
                  {answer.new_concerns.map((c, i) => (
                    <div key={i} className={styles.newConcern}>
                      [{c.severity}] {c.text}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
