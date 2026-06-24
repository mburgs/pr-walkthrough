import { useState } from "react";
import { useSession } from "../contexts/SessionContext";
import type { Flag } from "../contracts";
import styles from "./QuestionTracker.module.css";

export default function QuestionTracker() {
  const { flags } = useSession();

  return (
    <div className={styles.tracker}>
      <div className={styles.heading}>Flags ({flags.length})</div>
      {flags.length === 0 && (
        <div className={styles.empty}>No flags yet. Add one from Concerns tab.</div>
      )}
      {flags.map((flag) => (
        <FlagItem key={flag.flag_id} flag={flag} />
      ))}
    </div>
  );
}

function FlagItem({ flag }: { flag: Flag }) {
  const { updateFlag, postFlag, deleteFlag } = useSession();
  const [body, setBody] = useState(flag.body);
  const [posting, setPosting] = useState(false);
  const [localPosted, setLocalPosted] = useState(flag.posted);
  const [postedUrl, setPostedUrl] = useState(flag.posted_url);

  const handleBodyBlur = async () => {
    if (body !== flag.body) {
      await updateFlag(flag.flag_id, { body });
    }
  };

  const handlePost = async () => {
    setPosting(true);
    try {
      const updated = await postFlag(flag.flag_id);
      setLocalPosted(updated.posted);
      setPostedUrl(updated.posted_url);
    } finally {
      setPosting(false);
    }
  };

  const handleDelete = () => deleteFlag(flag.flag_id);

  return (
    <div className={styles.flag}>
      <div className={styles.flagHeader}>
        <span className={`${styles.severity} ${styles[flag.severity]}`}>{flag.severity}</span>
        {flag.anchor && (
          <span className={styles.anchor}>
            {flag.anchor.file}:{flag.anchor.line_range[0]}
          </span>
        )}
      </div>
      <textarea
        className={styles.body}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        onBlur={handleBodyBlur}
        disabled={localPosted}
      />
      <div className={styles.actions}>
        {!localPosted ? (
          <button className={styles.postBtn} onClick={handlePost} disabled={posting}>
            {posting ? "Posting…" : "Post to PR"}
          </button>
        ) : (
          <span style={{ fontSize: 11, color: "var(--ok)" }}>Posted</span>
        )}
        <button className={styles.deleteBtn} onClick={handleDelete}>
          Remove
        </button>
        {postedUrl && (
          <a
            className={styles.postedUrl}
            href={postedUrl}
            target="_blank"
            rel="noreferrer"
          >
            View on GitHub
          </a>
        )}
      </div>
    </div>
  );
}
