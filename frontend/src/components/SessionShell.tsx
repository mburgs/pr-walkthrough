
import { useSession } from "../contexts/SessionContext";
import ChunkList from "./ChunkList";
import DiffViewer from "./DiffViewer";
import NarrationPlayer from "./NarrationPlayer";
import SidePanel from "./SidePanel";
import FollowUpInput from "./FollowUpInput";
import styles from "./SessionShell.module.css";

export default function SessionShell() {
  const { session, currentChunkId, currentNarration, narrationLoading } = useSession();

  if (!session) return null;

  const currentChunk = session.plan.chunks.find((c) => c.chunk_id === currentChunkId) ?? null;

  return (
    <div className={styles.shell}>
      {/* Left: chunk list */}
      <aside className={styles.sidebar}>
        <div className={styles.sidebarHeader}>
          <div className={styles.prTitle} title={session.plan.pr.title}>
            {session.plan.pr.title}
          </div>
          <div className={styles.prMeta}>
            #{session.plan.pr.number} · {session.plan.pr.author} · {session.plan.pr.repo}
          </div>
        </div>
        <div className={styles.chunkList}>
          <ChunkList />
        </div>
      </aside>

      {/* Center: diff + narration */}
      <main className={styles.center}>
        <div className={styles.diffArea}>
          {currentChunk ? (
            <DiffViewer chunk={currentChunk} narration={currentNarration} />
          ) : (
            <div style={{ color: "var(--text-muted)", padding: 16 }}>Select a chunk to begin.</div>
          )}
        </div>
        <div className={styles.narrationArea}>
          {currentChunk && (
            <NarrationPlayer
              chunk={currentChunk}
              narration={currentNarration}
              loading={narrationLoading}
            />
          )}
        </div>
      </main>

      {/* Right: side panel */}
      <aside className={styles.rightPanel}>
        <SidePanel narration={currentNarration} />
      </aside>

      {/* Bottom: follow-up bar */}
      <div className={styles.followUpBar}>
        <FollowUpInput />
      </div>
    </div>
  );
}
