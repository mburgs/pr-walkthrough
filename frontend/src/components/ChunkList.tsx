
import { useSession } from "../contexts/SessionContext";
import styles from "./ChunkList.module.css";

export default function ChunkList() {
  const { session, currentChunkId, setCurrentChunkId } = useSession();
  if (!session) return null;

  return (
    <>
      {session.plan.chunks.map((chunk) => (
        <button
          key={chunk.chunk_id}
          className={`${styles.item} ${chunk.chunk_id === currentChunkId ? styles.active : ""}`}
          onClick={() => setCurrentChunkId(chunk.chunk_id)}
          title={chunk.rationale_for_position}
        >
          <div className={styles.chunkId}>{chunk.chunk_id}</div>
          <div className={styles.summary}>{chunk.summary}</div>
          <span className={`${styles.badge} ${styles[chunk.est_concern_level]}`}>
            {chunk.est_concern_level}
          </span>
        </button>
      ))}
    </>
  );
}
