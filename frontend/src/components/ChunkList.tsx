import { useSession } from "../contexts/SessionContext";
import styles from "./ChunkList.module.css";

interface Props {
  compact?: boolean;
}

export default function ChunkList({ compact = false }: Props) {
  const { session, currentChunkId, setCurrentChunkId } = useSession();
  if (!session) return null;

  return (
    <div className={compact ? styles.listCompact : styles.list}>
      {session.plan.chunks.map((chunk) => {
        const isActive = chunk.chunk_id === currentChunkId;
        if (compact) {
          return (
            <button
              key={chunk.chunk_id}
              className={`${styles.itemCompact} ${isActive ? styles.active : ""}`}
              onClick={() => setCurrentChunkId(chunk.chunk_id)}
              title={`${chunk.chunk_id.toUpperCase()} · ${chunk.summary}`}
            >
              <span className={styles.compactId}>{chunk.chunk_id.replace(/^c/, "")}</span>
              <span className={`${styles.dot} ${styles[chunk.est_concern_level]}`} />
            </button>
          );
        }
        return (
          <button
            key={chunk.chunk_id}
            className={`${styles.item} ${isActive ? styles.active : ""}`}
            onClick={() => setCurrentChunkId(chunk.chunk_id)}
            title={chunk.rationale_for_position}
          >
            <div className={styles.row}>
              <span className={styles.chunkId}>{chunk.chunk_id}</span>
              <span className={`${styles.badge} ${styles[chunk.est_concern_level]}`}>
                {chunk.est_concern_level}
              </span>
            </div>
            <div className={styles.summary}>{chunk.summary}</div>
          </button>
        );
      })}
    </div>
  );
}
