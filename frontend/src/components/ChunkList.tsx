import { Fragment } from "react";
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
      {session.plan.chunks.map((chunk, i) => {
        const isActive = chunk.chunk_id === currentChunkId;
        const prevGroup = i > 0 ? session.plan.chunks[i - 1].group ?? null : null;
        const groupChanged = (chunk.group ?? null) !== prevGroup;
        const showGroupHeader = !compact && !!chunk.group && groupChanged;

        if (compact) {
          // In the collapsed rail, separate groups with a thin gap rather than a label
          const showCompactDivider = !!chunk.group && groupChanged && i > 0;
          return (
            <Fragment key={chunk.chunk_id}>
              {showCompactDivider && <div className={styles.compactDivider} />}
              <button
                className={`${styles.itemCompact} ${isActive ? styles.active : ""}`}
                onClick={() => setCurrentChunkId(chunk.chunk_id)}
                title={`${chunk.chunk_id.toUpperCase()}${chunk.group ? ` · ${chunk.group}` : ""} · ${chunk.summary}`}
              >
                <span className={styles.compactId}>{chunk.chunk_id.replace(/^c/, "")}</span>
                <span className={`${styles.dot} ${styles[chunk.est_concern_level]}`} />
              </button>
            </Fragment>
          );
        }

        return (
          <Fragment key={chunk.chunk_id}>
            {showGroupHeader && (
              <div className={styles.groupHeader} aria-hidden>
                <span>{chunk.group}</span>
              </div>
            )}
            <button
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
          </Fragment>
        );
      })}
    </div>
  );
}
