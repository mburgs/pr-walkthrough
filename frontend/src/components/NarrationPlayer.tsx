import { useRef, useState, useEffect } from "react";
import type { TourChunk, ChunkNarration } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import { getAudioUrl } from "../api/client";
import styles from "./NarrationPlayer.module.css";

interface Props {
  chunk: TourChunk;
  narration: ChunkNarration | null;
  loading: boolean;
  /** Notified whenever the playhead crosses into a different segment. -1 = none. */
  onSegmentChange?: (segmentIndex: number) => void;
}

const SPEEDS = [1, 1.25, 1.5, 1.75, 2] as const;
const SPEED_STORAGE_KEY = "pr-walkthrough.playbackRate";

export default function NarrationPlayer({ chunk, narration, loading, onSegmentChange }: Props) {
  const { session, setCurrentChunkId, regenerateCurrentChunk, narrationGen } = useSession();
  const [menuOpen, setMenuOpen] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const playRef = useRef<HTMLDivElement>(null);

  // "Next chunk" target (null if we're already on the last chunk)
  const nextChunkId = (() => {
    const chunks = session?.plan.chunks ?? [];
    const idx = chunks.findIndex(c => c.chunk_id === chunk.chunk_id);
    return idx >= 0 && idx + 1 < chunks.length ? chunks[idx + 1].chunk_id : null;
  })();
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [pendingPlay, setPendingPlay] = useState(false);  // clicked play, audio still loading
  const [audioReady, setAudioReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeSegment, setActiveSegment] = useState<number>(-1);
  const [rate, setRate] = useState<number>(() => {
    const raw = Number(localStorage.getItem(SPEED_STORAGE_KEY));
    return SPEEDS.includes(raw as (typeof SPEEDS)[number]) ? raw : 1;
  });

  // Append narrationGen as a cache-bust so the audio element re-fetches
  // after a regenerate (same URL otherwise → browser plays stale bytes).
  const audioUrl = session
    ? `${getAudioUrl(session.plan.session_id, chunk.chunk_id)}?v=${narrationGen}`
    : null;

  const handleRegenerate = async () => {
    setMenuOpen(false);
    setRegenerating(true);
    try {
      await regenerateCurrentChunk();
    } finally {
      setRegenerating(false);
    }
  };

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return;
    const close = (e: MouseEvent) => {
      const t = e.target as Node;
      if (playRef.current && !playRef.current.contains(t)) setMenuOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [menuOpen]);

  // Apply rate to the live audio element and persist.
  // Depend on audioUrl too: HTMLAudioElement resets playbackRate to 1 when
  // src changes, so we re-assert the user's speed on every chunk swap.
  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = rate;
    localStorage.setItem(SPEED_STORAGE_KEY, String(rate));
  }, [rate, audioUrl]);

  const cycleRate = () => {
    const i = SPEEDS.indexOf(rate as (typeof SPEEDS)[number]);
    setRate(SPEEDS[(i + 1) % SPEEDS.length]);
  };

  // Reset state when chunk changes
  useEffect(() => {
    setPlaying(false);
    setPendingPlay(false);
    setAudioReady(false);
    setError(null);
    setActiveSegment(-1);
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
  }, [chunk.chunk_id]);

  // Notify parent of segment changes
  useEffect(() => { onSegmentChange?.(activeSegment); }, [activeSegment, onSegmentChange]);

  // Drive activeSegment from audio.currentTime + segment_offsets_ms
  const offsets = narration?.segment_offsets_ms ?? [];
  const handleTimeUpdate = () => {
    const audio = audioRef.current;
    if (!audio || offsets.length === 0) return;
    const ms = audio.currentTime * 1000;
    let idx = -1;
    for (let i = 0; i < offsets.length; i++) {
      if (offsets[i] <= ms) idx = i;
      else break;
    }
    if (idx !== activeSegment) setActiveSegment(idx);
  };

  const jumpToSegment = (i: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const t = offsets[i];
    if (t === undefined) return;
    audio.currentTime = t / 1000;
    setActiveSegment(i);
    if (!playing) {
      audio.play().catch((e) => setError(String(e)));
      setPlaying(true);
    }
  };

  const handlePlay = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      audio.pause();
      setPlaying(false);
      setPendingPlay(false);
      return;
    }
    // If the audio isn't loaded yet (backend still synthesising), latch the
    // user's intent so playback kicks off the moment the data arrives.
    if (!audioReady) { setPendingPlay(true); return; }
    audio.play().catch((e) => setError(String(e)));
    setPlaying(true);
  };

  const handleCanPlay = () => {
    setAudioReady(true);
    // If the user already clicked play, honour it now
    if (pendingPlay) {
      const audio = audioRef.current;
      if (audio) {
        audio.play().catch((e) => setError(String(e)));
        setPlaying(true);
      }
      setPendingPlay(false);
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
    if (nextChunkId) setCurrentChunkId(nextChunkId);
  };

  const handleEnded = () => setPlaying(false);
  const handleError = () => { setError("Audio failed to load"); setPlaying(false); };

  return (
    <div className={styles.player}>
      {loading && (
        <div className={styles.loading}>
          <span style={{ width: 6, height: 6, borderRadius: 3, background: "var(--accent)", display: "inline-block", animation: "pulse 1.4s infinite" }} />
          narrating…
        </div>
      )}
      {!loading && narration && narration.segments.length > 0 ? (
        <div className={styles.script}>
          {narration.segments.map((seg, i) => (
            <span
              key={i}
              className={`${styles.segment} ${i === activeSegment ? styles.segmentActive : ""} ${seg.anchor ? styles.segmentAnchored : ""}`}
              onClick={() => jumpToSegment(i)}
              role="button"
              tabIndex={0}
              title={seg.anchor ? `Jump to ${seg.anchor.file}:${seg.anchor.line_range[0]}` : "Jump to this segment"}
            >
              {seg.text}{" "}
            </span>
          ))}
        </div>
      ) : !loading && narration ? (
        <div className={styles.script}>{narration.narration}</div>
      ) : null}

      <div className={styles.controls}>
        <div className={styles.playGroup} ref={playRef}>
          <button className={`${styles.btn} ${styles.playBtn} ${styles.playBtnMain}`} onClick={handlePlay} disabled={loading || !narration || regenerating}>
            {regenerating
              ? <><span className={styles.spinner} aria-hidden /> regenerating…</>
              : pendingPlay
                ? <><span className={styles.spinner} aria-hidden /> buffering…</>
                : playing
                  ? "⏸ Pause"
                  : "▶ Play"}
          </button>
          <button
            className={`${styles.btn} ${styles.playBtn} ${styles.playBtnCaret}`}
            onClick={() => setMenuOpen(v => !v)}
            disabled={loading || regenerating}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            title="More actions"
          >▾</button>
          {menuOpen && (
            <div className={styles.menu} role="menu">
              <button
                className={styles.menuItem}
                onClick={handleRegenerate}
                role="menuitem"
              >
                <span style={{ display: "inline-block", width: 16 }}>↻</span>
                Regenerate this chunk
                <div className={styles.menuItemHint}>Wipes the narration + audio and re-runs the prompt.</div>
              </button>
            </div>
          )}
        </div>
        <button className={styles.btn} onClick={handleReplay} disabled={loading || !narration}>↺</button>
        <button
          className={styles.btn}
          onClick={handleSkip}
          disabled={!nextChunkId}
          title={nextChunkId ? `Next chunk (${nextChunkId})` : "Last chunk"}
        >⏭</button>
        <button
          className={styles.speedBtn}
          onClick={cycleRate}
          title="Playback speed"
          aria-label={`Playback speed ${rate}×`}
        >{rate}×</button>
        {error && <span className={styles.errorNote}>{error}</span>}
        <span className={styles.chunkLabel}>{chunk.chunk_id}</span>
      </div>

      {audioUrl && (
        <audio
          ref={audioRef}
          src={audioUrl}
          onEnded={handleEnded}
          onError={handleError}
          onTimeUpdate={handleTimeUpdate}
          onSeeked={handleTimeUpdate}
          onCanPlay={handleCanPlay}
          preload="auto"
          style={{ display: "none" }}
        />
      )}
    </div>
  );
}
