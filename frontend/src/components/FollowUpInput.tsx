import { useState, useRef, useEffect } from "react";
import type { FollowUpAnswer } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import styles from "./FollowUpInput.module.css";

export default function FollowUpInput() {
  const { session, submitFollowUp } = useSession();
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [answer, setAnswer] = useState<FollowUpAnswer | null>(null);
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

  const handleSubmit = async () => {
    const q = text.trim();
    if (!q || !session) return;
    setSubmitting(true);
    setAnswer(null);
    try {
      const ans = await submitFollowUp(q);
      setAnswer(ans);
      setText("");
    } finally {
      setSubmitting(false);
    }
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
        setSubmitting(true);
        setAnswer(null);
        try {
          const ans = await submitFollowUp("", blob);
          setAnswer(ans);
        } finally {
          setSubmitting(false);
        }
      };
      recorder.start();
      recorderRef.current = recorder;
      setRecording(true);
    } catch {
      setMicAvailable(false);
    }
  };

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
          disabled={submitting || !session}
        />
        <button
          className={styles.btn}
          onClick={handleSubmit}
          disabled={!text.trim() || submitting || !session}
        >
          {submitting ? "…" : "Ask"}
        </button>
        {micAvailable !== false && (
          <button
            className={`${styles.micBtn} ${recording ? styles.recording : ""}`}
            onClick={toggleRecording}
            disabled={submitting || !session}
            title={recording ? "Stop recording" : "Push to talk"}
          >
            {recording ? "⏹" : "🎙"}
          </button>
        )}
      </div>

      {answer && (
        <div className={styles.answer}>
          <div className={styles.answerLabel}>Answer</div>
          <div className={styles.answerText}>{answer.answer_text}</div>
          {answer.new_concerns.length > 0 && (
            <div className={styles.newConcerns}>
              {answer.new_concerns.map((c, i) => (
                <div key={i} className={styles.newConcern}>
                  [{c.severity}] {c.text}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
