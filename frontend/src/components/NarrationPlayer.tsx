import { useRef, useState, useEffect } from "react";
import type { TourChunk, ChunkNarration } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import { getAudioUrl } from "../api/client";
import styles from "./NarrationPlayer.module.css";

interface Props {
  chunk: TourChunk;
  narration: ChunkNarration | null;
  loading: boolean;
}

export default function NarrationPlayer({ chunk, narration, loading }: Props) {
  const { session } = useSession();
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const audioUrl = session
    ? getAudioUrl(session.plan.session_id, chunk.chunk_id)
    : null;

  // Reset state when chunk changes
  useEffect(() => {
    setPlaying(false);
    setError(null);
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
  }, [chunk.chunk_id]);

  const handlePlay = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      audio.pause();
      setPlaying(false);
    } else {
      audio.play().catch((e) => setError(String(e)));
      setPlaying(true);
    }
  };

  const handleReplay = () => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = 0;
    audio.play().catch((e) => setError(String(e)));
    setPlaying(true);
  };

  const handleSkip = () => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = audio.duration;
    setPlaying(false);
  };

  const handleEnded = () => setPlaying(false);
  const handleError = () => {
    setError("Audio unavailable (using silent stub in dev)");
    setPlaying(false);
  };

  return (
    <div className={styles.player}>
      {loading && (
        <div className={styles.loading}>
          <span style={{ width: 6, height: 6, borderRadius: 3, background: "var(--accent)", display: "inline-block", animation: "pulse 1.4s infinite" }} />
          narrating…
        </div>
      )}
      {!loading && narration && (
        <div className={styles.script}>{narration.narration}</div>
      )}

      <div className={styles.controls}>
        <button className={`${styles.btn} ${styles.playBtn}`} onClick={handlePlay} disabled={loading || !narration}>
          {playing ? "⏸ Pause" : "▶ Play"}
        </button>
        <button className={styles.btn} onClick={handleReplay} disabled={loading || !narration}>
          ↺
        </button>
        <button className={styles.btn} onClick={handleSkip} disabled={loading || !narration || !playing}>
          ⏭
        </button>
        {error && <span className={styles.errorNote}>{error}</span>}
        <span className={styles.chunkLabel}>{chunk.chunk_id}</span>
      </div>

      {audioUrl && (
        <audio
          ref={audioRef}
          src={audioUrl}
          onEnded={handleEnded}
          onError={handleError}
          style={{ display: "none" }}
        />
      )}
    </div>
  );
}
