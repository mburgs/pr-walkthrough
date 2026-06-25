import { useRef, useState, useEffect } from "react";
import type { TourChunk, ChunkNarration } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import { fetchVariant, getAvailableEngines, getAudioUrl } from "../api/client";
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
const ENGINE_STORAGE_KEY = "pr-walkthrough.ttsEngine";
const FILTER_STORAGE_KEY = "pr-walkthrough.ttsFiltered";

const ENGINE_LABELS: Record<string, string> = {
  kokoro: "Kokoro",
  xtts: "XTTS-v2",
  f5: "F5-TTS",
  say: "macOS Say",
  piper: "Piper",
};

export default function NarrationPlayer({ chunk, narration, loading, onSegmentChange }: Props) {
  const { session } = useSession();
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeSegment, setActiveSegment] = useState<number>(-1);
  const [rate, setRate] = useState<number>(() => {
    const raw = Number(localStorage.getItem(SPEED_STORAGE_KEY));
    return SPEEDS.includes(raw as (typeof SPEEDS)[number]) ? raw : 1;
  });

  // ----- Variant switchers -----
  const [engines, setEngines] = useState<string[]>([]);
  const [engine, setEngine] = useState<string>(
    () => localStorage.getItem(ENGINE_STORAGE_KEY) || "kokoro"
  );
  const [filtered, setFiltered] = useState<boolean>(
    () => localStorage.getItem(FILTER_STORAGE_KEY) !== "false"
  );
  // The audio src for the currently selected variant; null while loading.
  const [variantUrl, setVariantUrl] = useState<string | null>(null);
  const [variantOffsets, setVariantOffsets] = useState<number[]>([]);
  const [variantLoading, setVariantLoading] = useState(false);

  // Client-side cache so already-loaded variants switch instantly (no refetch,
  // no blob recreate, no audio element reload). Keyed by `${chunk}/${engine}/${filtered}`.
  const variantCache = useRef<Map<string, { blobUrl: string; offsetsMs: number[] }>>(new Map());
  const variantKey = (cid: string, eng: string, filt: boolean) => `${cid}/${eng}/${filt}`;

  // Discover which engines the backend can offer (once per chunk)
  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    getAvailableEngines(session.plan.session_id, chunk.chunk_id)
      .then(r => { if (!cancelled) setEngines(r.engines); })
      .catch(() => { if (!cancelled) setEngines([]); });
    return () => { cancelled = true; };
  }, [session, chunk.chunk_id]);

  // Load the requested variant whenever engine / filter / chunk changes.
  // Cache hits switch instantly; misses kick off a fetch.
  useEffect(() => {
    if (!session || !narration) return;
    let cancelled = false;

    const key = variantKey(chunk.chunk_id, engine, filtered);
    const cached = variantCache.current.get(key);
    if (cached) {
      setError(null);
      setVariantLoading(false);
      setVariantUrl(cached.blobUrl);
      setVariantOffsets(cached.offsetsMs.length ? cached.offsetsMs : (narration.segment_offsets_ms ?? []));
      return;
    }

    setVariantLoading(true);
    setError(null);
    // Keep the old src playing while we fetch the new one — feels less jarring
    // than going silent. The audio element only resets when the src actually changes.

    fetchVariant(session.plan.session_id, chunk.chunk_id, engine, filtered)
      .then(v => {
        if (cancelled) return;
        if (!v) { setError(`Failed to load ${engine} (${filtered ? "filtered" : "raw"})`); return; }
        variantCache.current.set(key, v);
        setVariantUrl(v.blobUrl);
        setVariantOffsets(v.offsetsMs.length ? v.offsetsMs : (narration.segment_offsets_ms ?? []));
      })
      .catch(e => { if (!cancelled) setError(String(e)); })
      .finally(() => { if (!cancelled) setVariantLoading(false); });

    return () => { cancelled = true; };
  }, [session, narration, chunk.chunk_id, engine, filtered]);

  // Free blob URLs when this player unmounts (e.g. switching chunks).
  useEffect(() => {
    const cache = variantCache.current;
    return () => {
      cache.forEach(v => URL.revokeObjectURL(v.blobUrl));
      cache.clear();
    };
  }, [chunk.chunk_id]);

  // Persist switcher choices
  useEffect(() => { localStorage.setItem(ENGINE_STORAGE_KEY, engine); }, [engine]);
  useEffect(() => { localStorage.setItem(FILTER_STORAGE_KEY, String(filtered)); }, [filtered]);

  // Apply rate to the live audio element and persist
  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = rate;
    localStorage.setItem(SPEED_STORAGE_KEY, String(rate));
  }, [rate]);

  const cycleRate = () => {
    const i = SPEEDS.indexOf(rate as (typeof SPEEDS)[number]);
    setRate(SPEEDS[(i + 1) % SPEEDS.length]);
  };

  // Reset state when chunk changes
  useEffect(() => {
    setPlaying(false);
    setError(null);
    setActiveSegment(-1);
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
  }, [chunk.chunk_id]);

  // Notify parent of segment changes
  useEffect(() => { onSegmentChange?.(activeSegment); }, [activeSegment, onSegmentChange]);

  // Drive activeSegment from audio.currentTime + variantOffsets
  const handleTimeUpdate = () => {
    const audio = audioRef.current;
    if (!audio || variantOffsets.length === 0) return;
    const ms = audio.currentTime * 1000;
    let idx = -1;
    for (let i = 0; i < variantOffsets.length; i++) {
      if (variantOffsets[i] <= ms) idx = i;
      else break;
    }
    if (idx !== activeSegment) setActiveSegment(idx);
  };

  const jumpToSegment = (i: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const t = variantOffsets[i];
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
    if (playing) { audio.pause(); setPlaying(false); }
    else { audio.play().catch((e) => setError(String(e))); setPlaying(true); }
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
  const handleError = () => { setError("Audio failed to load"); setPlaying(false); };

  // Fallback to the legacy default-variant URL while no audio.variant has loaded
  const playableSrc = variantUrl ?? (session ? getAudioUrl(session.plan.session_id, chunk.chunk_id) : null);

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
        <button className={`${styles.btn} ${styles.playBtn}`} onClick={handlePlay} disabled={loading || !narration || variantLoading}>
          {variantLoading ? "…" : playing ? "⏸ Pause" : "▶ Play"}
        </button>
        <button className={styles.btn} onClick={handleReplay} disabled={loading || !narration || variantLoading}>↺</button>
        <button className={styles.btn} onClick={handleSkip} disabled={loading || !narration || !playing}>⏭</button>
        <button
          className={styles.speedBtn}
          onClick={cycleRate}
          title="Playback speed"
          aria-label={`Playback speed ${rate}×`}
        >{rate}×</button>

        {/* Variant switchers */}
        <div className={styles.variantBar}>
          <select
            className={styles.select}
            value={engine}
            onChange={e => setEngine(e.target.value)}
            disabled={engines.length === 0}
            title="TTS engine"
          >
            {(engines.length ? engines : [engine]).map(e => (
              <option key={e} value={e}>{ENGINE_LABELS[e] ?? e}</option>
            ))}
          </select>
          <button
            className={styles.filterBtn}
            onClick={() => setFiltered(v => !v)}
            title="Filtered: TTS-friendly text scrubbing (slashes, backticks). Raw: original."
          >{filtered ? "filtered" : "raw"}</button>
        </div>

        {error && <span className={styles.errorNote}>{error}</span>}
        <span className={styles.chunkLabel}>{chunk.chunk_id}</span>
      </div>

      {playableSrc && (
        <audio
          ref={audioRef}
          src={playableSrc}
          onEnded={handleEnded}
          onError={handleError}
          onTimeUpdate={handleTimeUpdate}
          onSeeked={handleTimeUpdate}
          style={{ display: "none" }}
        />
      )}
    </div>
  );
}
