import { useRef, useState, useEffect } from "react";
import type { ChangeEvent, CSSProperties, SyntheticEvent } from "react";
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
  /** Mini mode for the collapsed rail — just play/skip/speed, no script. */
  compact?: boolean;
}

const SPEEDS = [1, 1.25, 1.5, 1.75, 2] as const;
const SPEED_STORAGE_KEY = "pr-walkthrough.playbackRate";

export default function NarrationPlayer({ chunk, narration, loading, onSegmentChange, compact = false }: Props) {
  const {
    session,
    setCurrentChunkId,
    regenerateCurrentChunk,
    narrationGen,
    activeLevel,
    setActiveLevel,
    chunkPhases,
  } = useSession();
  const phase = chunkPhases[chunk.chunk_id];
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
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [scrubbing, setScrubbing] = useState(false);
  const [rate, setRate] = useState<number>(() => {
    const raw = Number(localStorage.getItem(SPEED_STORAGE_KEY));
    return SPEEDS.includes(raw as (typeof SPEEDS)[number]) ? raw : 1;
  });
  // Script visibility — collapsed by default so the player is just
  // audio + scrub bar. MUST live at the top of the component (above
  // the `if (compact)` early return), otherwise toggling the rail's
  // collapse changes the hook call order and React unmounts the
  // whole tree.
  const [scriptOpen, setScriptOpen] = useState(false);

  // Append this chunk's gen counter as a cache-bust so the audio element
  // re-fetches after a regenerate (same URL otherwise → browser plays stale
  // bytes). Keyed per chunk so regenerating c1 doesn't invalidate c2's
  // already-cached audio blob in the browser.
  const chunkGen = narrationGen[chunk.chunk_id] ?? 0;
  // Audio URL embeds activeLevel so swapping the level re-fetches the
  // per-level WAV (the backend keys audio_bytes by (sid, cid, level)).
  // `?v=` is the regenerate cache-bust; `&level=` selects the variant.
  const audioUrl = session
    ? `${getAudioUrl(session.plan.session_id, chunk.chunk_id, activeLevel)}&v=${chunkGen}`
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
    setCurrentTime(0);
    setDuration(0);
    setScrubbing(false);
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
    if (!audio) return;
    if (!scrubbing) setCurrentTime(audio.currentTime);
    if (offsets.length === 0) return;
    const ms = audio.currentTime * 1000;
    let idx = -1;
    for (let i = 0; i < offsets.length; i++) {
      if (offsets[i] <= ms) idx = i;
      else break;
    }
    if (idx !== activeSegment) setActiveSegment(idx);
  };

  const handleLoadedMetadata = () => {
    const audio = audioRef.current;
    if (!audio) return;
    // Safari can report Infinity for blob-served audio until first play; treat as 0.
    setDuration(Number.isFinite(audio.duration) ? audio.duration : 0);
  };

  const handleDurationChange = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (Number.isFinite(audio.duration)) setDuration(audio.duration);
  };

  const seekTo = (seconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const clamped = Math.max(0, Math.min(seconds, duration || audio.duration || 0));
    audio.currentTime = clamped;
    setCurrentTime(clamped);
  };

  const handleScrubInput = (e: ChangeEvent<HTMLInputElement>) => {
    // Live preview the new time as the user drags, but don't seek the
    // audio element until they release — seeking on every input event
    // makes Safari stutter and racks up onSeeked events that re-trigger
    // segment recompute mid-drag.
    setCurrentTime(Number(e.target.value));
  };

  const handleScrubCommit = (e: SyntheticEvent<HTMLInputElement>) => {
    setScrubbing(false);
    seekTo(Number((e.target as HTMLInputElement).value));
  };

  const formatTime = (secs: number) => {
    if (!Number.isFinite(secs) || secs < 0) secs = 0;
    const total = Math.floor(secs);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  };

  const remaining = Math.max(0, (duration || 0) - currentTime);

  // Re-assert rate then play. The browser silently resets playbackRate
  // back to 1 across src loads (and sometimes between mount and first
  // play), so simply trusting the [rate, audioUrl] effect lets the
  // first playback after a page reload run at 1×. Setting rate
  // *immediately before* play() closes that window in every code path.
  const startPlay = (audio: HTMLAudioElement) => {
    audio.playbackRate = rate;
    audio.play().catch((e) => setError(String(e)));
    setPlaying(true);
  };

  const jumpToSegment = (i: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const t = offsets[i];
    if (t === undefined) return;
    audio.currentTime = t / 1000;
    setActiveSegment(i);
    if (!playing) startPlay(audio);
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
    startPlay(audio);
  };

  const handleCanPlay = () => {
    setAudioReady(true);
    const audio = audioRef.current;
    if (audio) audio.playbackRate = rate;
    if (pendingPlay) {
      if (audio) startPlay(audio);
      setPendingPlay(false);
    }
  };

  const handleSkip = () => {
    if (nextChunkId) setCurrentChunkId(nextChunkId);
  };

  const handleEnded = () => setPlaying(false);
  const handleError = () => { setError("Audio failed to load"); setPlaying(false); };

  // The audio element is rendered ONCE at the end as a stable sibling
  // of whichever UI mode we're in. That way, toggling compact↔full
  // doesn't unmount the <audio> tag, so playback keeps going across the
  // user collapsing the rail. Both UI modes are wrapped in branches
  // that React will remount, but the audio sibling persists.
  const audioEl = audioUrl ? (
    <audio
      ref={audioRef}
      src={audioUrl}
      onEnded={handleEnded}
      onError={handleError}
      onTimeUpdate={handleTimeUpdate}
      onSeeked={handleTimeUpdate}
      onCanPlay={handleCanPlay}
      onLoadedMetadata={handleLoadedMetadata}
      onDurationChange={handleDurationChange}
      preload="auto"
      style={{ display: "none" }}
    />
  ) : null;

  const scrubMax = duration > 0 ? duration : 0;
  const scrubDisabled = !audioReady || scrubMax <= 0;
  const scrubPct = scrubMax > 0 ? (currentTime / scrubMax) * 100 : 0;
  const scrubBar = (
    <div className={styles.scrub}>
      <span className={styles.time}>{formatTime(currentTime)}</span>
      <input
        type="range"
        className={styles.scrubInput}
        min={0}
        max={scrubMax || 1}
        step={0.01}
        value={Math.min(currentTime, scrubMax || 0)}
        disabled={scrubDisabled}
        onMouseDown={() => setScrubbing(true)}
        onTouchStart={() => setScrubbing(true)}
        onChange={handleScrubInput}
        onMouseUp={handleScrubCommit}
        onTouchEnd={handleScrubCommit}
        onKeyDown={(e) => {
          // Arrow keys nudge ±5s; commit immediately
          if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
            e.preventDefault();
            const delta = e.key === "ArrowLeft" ? -5 : 5;
            seekTo(currentTime + delta);
          }
        }}
        style={{ "--scrub-pct": `${scrubPct}%` } as CSSProperties}
        aria-label="Audio position"
      />
      <span className={styles.time}>-{formatTime(remaining)}</span>
    </div>
  );

  if (compact) {
    return (
      <>
        <div className={styles.miniPlayer}>
          <button
            className={styles.miniBtn}
            onClick={handlePlay}
            disabled={loading || !narration || regenerating}
            title={playing ? "Pause" : "Play"}
            aria-label={playing ? "Pause" : "Play"}
          >
            {pendingPlay
              ? <span className={styles.spinner} aria-hidden />
              : playing ? "⏸" : "▶"}
          </button>
          <button
            className={styles.miniBtn}
            onClick={handleSkip}
            disabled={!nextChunkId}
            title={nextChunkId ? `Next chunk (${nextChunkId})` : "Last chunk"}
            aria-label="Next chunk"
          >⏭</button>
          <button
            className={styles.miniSpeed}
            onClick={cycleRate}
            title="Playback speed"
            aria-label={`Playback speed ${rate}×`}
          >{rate}×</button>
          <span className={styles.miniChunk}>{chunk.chunk_id}</span>
          <div
            className={styles.miniProgress}
            style={{ "--scrub-pct": `${scrubPct}%` } as CSSProperties}
            title={`${formatTime(currentTime)} / ${formatTime(duration)}`}
            aria-hidden
          />
        </div>
        {audioEl}
      </>
    );
  }

  return (
    <>
    <div className={styles.player}>
      {loading && <PhaseProgress phase={phase} />}
      {!loading && narration && (
        <div className={styles.scriptToggleRow}>
          <button
            type="button"
            className={styles.scriptToggle}
            onClick={() => setScriptOpen((v) => !v)}
            aria-expanded={scriptOpen}
          >
            {scriptOpen ? "▾ hide script" : "▸ show script"}
          </button>
        </div>
      )}
      {!loading && narration && scriptOpen && narration.segments.length > 0 ? (
        <div className={styles.script}>
          {narration.segments.map((seg, i) => (
            <span
              key={i}
              className={`${styles.segment} ${i === activeSegment ? styles.segmentActive : ""} ${seg.anchor ? styles.segmentAnchored : ""}`}
              onClick={() => jumpToSegment(i)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  jumpToSegment(i);
                }
              }}
              role="button"
              tabIndex={0}
              title={seg.anchor ? `Jump to ${seg.anchor.file}:${seg.anchor.line_range[0]}` : "Jump to this segment"}
            >
              {seg.text}{" "}
            </span>
          ))}
        </div>
      ) : !loading && narration && scriptOpen ? (
        <div className={styles.script}>{narration.narration}</div>
      ) : null}

      {session?.plan.multi_level && (
        <div className={styles.levelSwitcher} role="radiogroup" aria-label="Narration depth">
          {(["tutorial","tour","review","highlights"] as const).map(lvl => (
            <button
              key={lvl}
              type="button"
              role="radio"
              aria-checked={activeLevel === lvl}
              className={`${styles.levelChip} ${activeLevel === lvl ? styles.levelChipActive : ""}`}
              onClick={() => setActiveLevel(lvl)}
              title={`Switch to ${lvl} depth`}
            >
              {lvl}
            </button>
          ))}
        </div>
      )}

      {scrubBar}

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

    </div>
    {audioEl}
    </>
  );
}

// ── Phase progress indicator ─────────────────────────────────────────────────
//
// Renders three steps — narrating → analyzing → synthesizing — with the
// step matching the current backend phase highlighted. Anchor pass and
// TTS overlap on the backend, but for the viewer we render them as
// sequential steps in reading order (analyzing precedes synthesizing on
// the bar even though both kick off at the same time). The latest phase
// event wins, so once TTS starts the bar advances to "synthesizing"
// regardless of whether the anchor pass has finished.

const PHASE_STEPS = [
  { key: "narrating", label: "narrating" },
  { key: "anchoring", label: "analyzing" },
  { key: "synthesizing", label: "synthesizing" },
] as const;

function PhaseProgress({ phase }: { phase: string | undefined }) {
  // Map "ready" to last step (the loading view shouldn't render in ready
  // state but guard against late events). Unknown / undefined phase →
  // show step 0 pulsing.
  const activeIdx = (() => {
    if (!phase) return 0;
    if (phase === "ready") return PHASE_STEPS.length - 1;
    const i = PHASE_STEPS.findIndex((s) => s.key === phase);
    return i >= 0 ? i : 0;
  })();
  return (
    <div className={styles.phaseProgress} role="status" aria-live="polite">
      {PHASE_STEPS.map((step, i) => {
        const done = i < activeIdx;
        const active = i === activeIdx;
        return (
          <div
            key={step.key}
            className={`${styles.phaseStep} ${done ? styles.phaseStepDone : ""} ${active ? styles.phaseStepActive : ""}`}
          >
            <span className={styles.phaseDot} aria-hidden />
            <span className={styles.phaseLabel}>{step.label}</span>
            {i < PHASE_STEPS.length - 1 && <span className={styles.phaseConnector} aria-hidden />}
          </div>
        );
      })}
    </div>
  );
}
