import { useState } from "react";
import type { ChunkNarration, Concern } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import styles from "./SidePanel.module.css";

type Tab = "highlights" | "related" | "concerns" | "look_closer";

interface Props {
  narration: ChunkNarration | null;
}

export default function SidePanel({ narration }: Props) {
  const [tab, setTab] = useState<Tab>("concerns");

  const tabs: { id: Tab; label: string }[] = [
    { id: "highlights", label: "Highlights" },
    { id: "related", label: "Related" },
    { id: "concerns", label: "Concerns" },
    { id: "look_closer", label: "Look" },
  ];

  return (
    <div className={styles.panel}>
      <div className={styles.tabs}>
        {tabs.map((t) => (
          <button
            key={t.id}
            className={`${styles.tab} ${tab === t.id ? styles.active : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className={styles.content}>
        {!narration && (
          <div className={styles.empty}>No data yet</div>
        )}
        {narration && tab === "highlights" && <HighlightsTab narration={narration} />}
        {narration && tab === "related" && <RelatedTab narration={narration} />}
        {narration && tab === "concerns" && <ConcernsTab narration={narration} />}
        {narration && tab === "look_closer" && <LookTab narration={narration} />}
      </div>
    </div>
  );
}

function HighlightsTab({ narration }: { narration: ChunkNarration }) {
  if (narration.highlights.length === 0) {
    return <div className={styles.empty}>No highlights</div>;
  }
  return (
    <>
      {narration.highlights.map((h, i) => (
        <div key={i} className={styles.item}>
          <div className={styles.anchor}>{h.anchor.file}:{h.anchor.line_range[0]}–{h.anchor.line_range[1]}</div>
          <div className={styles.itemText}>{h.why}</div>
        </div>
      ))}
    </>
  );
}

function RelatedTab({ narration }: { narration: ChunkNarration }) {
  if (narration.related_code.length === 0) {
    return <div className={styles.empty}>No related code</div>;
  }
  return (
    <>
      {narration.related_code.map((r, i) => (
        <div key={i} className={styles.item}>
          <div className={styles.itemHeader}>
            <span className={styles.relationship}>{r.relationship}</span>
            <span className={styles.anchor}>{r.anchor.file}:{r.anchor.line_range[0]}–{r.anchor.line_range[1]}</span>
          </div>
          <pre className={styles.snippet}>{r.snippet}</pre>
        </div>
      ))}
    </>
  );
}

function ConcernsTab({ narration }: { narration: ChunkNarration }) {
  if (narration.concerns.length === 0) {
    return <div className={styles.empty}>No concerns flagged</div>;
  }
  return (
    <>
      {narration.concerns.map((c, i) => (
        <ConcernItem key={i} concern={c} chunkId={narration.chunk_id} />
      ))}
    </>
  );
}

function ConcernItem({ concern, chunkId }: { concern: Concern; chunkId: string }) {
  const { addFlag } = useSession();
  const [added, setAdded] = useState(false);

  const handleAddFlag = async () => {
    await addFlag({
      chunk_id: chunkId,
      anchor: concern.anchor,
      severity: concern.severity,
      body: concern.suggested_question,
    });
    setAdded(true);
  };

  return (
    <div className={styles.item}>
      <div className={styles.itemHeader}>
        <span className={`${styles.severity} ${styles[concern.severity]}`}>{concern.severity}</span>
        {concern.anchor && (
          <span className={styles.anchor}>
            {concern.anchor.file}:{concern.anchor.line_range[0]}
          </span>
        )}
      </div>
      <div className={styles.itemText}>{concern.text}</div>
      <div className={styles.question}>{concern.suggested_question}</div>
      {!added ? (
        <button className={styles.addFlagBtn} onClick={handleAddFlag}>
          + Add to flags
        </button>
      ) : (
        <span style={{ fontSize: 11, color: "var(--ok)" }}>Added to flags</span>
      )}
    </div>
  );
}

function LookTab({ narration }: { narration: ChunkNarration }) {
  if (narration.look_closer_for.length === 0) {
    return <div className={styles.empty}>Nothing flagged to look closer at</div>;
  }
  return (
    <>
      {narration.look_closer_for.map((item, i) => (
        <div key={i} className={styles.lookItem}>{item}</div>
      ))}
    </>
  );
}
