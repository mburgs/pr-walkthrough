import { useState, useEffect, useCallback } from "react";
import type { CodeAnchor } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import ChunkList from "./ChunkList";
import DiffViewer from "./DiffViewer";
import RightRail from "./RightRail";
import FollowUpInput from "./FollowUpInput";
import styles from "./SessionShell.module.css";

// Persist rail-collapse state across reloads. localStorage keys are
// stable across PRs / sessions — the layout preference is the user's,
// not the session's.
const LEFT_COLLAPSED_KEY = "pr-walkthrough.leftCollapsed";
const RIGHT_COLLAPSED_KEY = "pr-walkthrough.rightCollapsed";

function loadBool(key: string): boolean {
  try { return localStorage.getItem(key) === "1"; } catch { return false; }
}

export default function SessionShell() {
  const { session, currentChunkId, currentNarration, narrationLoading } = useSession();
  const [leftCollapsed, setLeftCollapsed] = useState<boolean>(() => loadBool(LEFT_COLLAPSED_KEY));
  const [rightCollapsed, setRightCollapsed] = useState<boolean>(() => loadBool(RIGHT_COLLAPSED_KEY));
  useEffect(() => { localStorage.setItem(LEFT_COLLAPSED_KEY, leftCollapsed ? "1" : "0"); }, [leftCollapsed]);
  useEffect(() => { localStorage.setItem(RIGHT_COLLAPSED_KEY, rightCollapsed ? "1" : "0"); }, [rightCollapsed]);
  // Active diff anchor — last-writer-wins between audio segment progression
  // and manual side-panel clicks. The DiffViewer reacts to whatever is here.
  const [activeAnchor, setActiveAnchor] = useState<CodeAnchor | null>(null);

  // Reset when chunk changes (player and side panel both reset on their own).
  useEffect(() => { setActiveAnchor(null); }, [currentChunkId]);

  // useCallback so NarrationPlayer's onSegmentChange-watching effect doesn't
  // re-fire on every parent render and clobber a click-driven anchor.
  const handleSegmentChange = useCallback((idx: number) => {
    setActiveAnchor(
      idx >= 0 ? (currentNarration?.segments?.[idx]?.anchor ?? null) : null
    );
  }, [currentNarration]);

  if (!session) return null;

  const currentChunk =
    session.plan.chunks.find((c) => c.chunk_id === currentChunkId) ?? null;

  const shellClass = [
    styles.shell,
    leftCollapsed ? styles.leftCollapsed : "",
    rightCollapsed ? styles.rightCollapsed : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={shellClass}>
      <aside className={styles.sidebar}>
        <div className={styles.sidebarHeader}>
          <div className={styles.sidebarHeaderMain}>
            <div className={styles.prTitle} title={session.plan.pr.title}>
              {session.plan.pr.title}
            </div>
            <div className={styles.prMeta}>
              {session.plan.pr.repo} · #{session.plan.pr.number}
            </div>
          </div>
          <button
            className={styles.collapseBtn}
            onClick={() => setLeftCollapsed(true)}
            title="Collapse chunk list"
            aria-label="Collapse chunk list"
          >‹</button>
        </div>
        {leftCollapsed && (
          <button
            className={styles.collapseBtn}
            onClick={() => setLeftCollapsed(false)}
            title="Expand chunk list"
            aria-label="Expand chunk list"
            style={{ margin: "10px auto 4px" }}
          >›</button>
        )}
        <div className={styles.chunkList}>
          <ChunkList compact={leftCollapsed} />
        </div>
      </aside>

      <main className={styles.center}>
        <div className={styles.diffArea}>
          {currentChunk ? (
            <DiffViewer chunk={currentChunk} activeAnchor={activeAnchor} />
          ) : (
            <div className={styles.emptyCenter}>Select a chunk to begin.</div>
          )}
        </div>
      </main>

      <aside className={styles.rightPanel}>
        <RightRail
          chunk={currentChunk}
          narration={currentNarration}
          narrationLoading={narrationLoading}
          collapsed={rightCollapsed}
          onToggle={() => setRightCollapsed((v) => !v)}
          onSegmentChange={handleSegmentChange}
          onAnchorClick={setActiveAnchor}
          activeAnchor={activeAnchor}
        />
      </aside>

      <div className={styles.followUpBar}>
        <FollowUpInput />
      </div>
    </div>
  );
}
