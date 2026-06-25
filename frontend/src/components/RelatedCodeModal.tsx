import { Fragment, useEffect, useRef, useState } from "react";
import type { RelatedCode } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import { getRepoFile } from "../api/client";
import { highlightSnippet, languageFor, renderHast } from "../lib/highlight";
import styles from "./RelatedCodeModal.module.css";

interface Props {
  related: RelatedCode;
  onClose: () => void;
}

/**
 * Modal that shows the **full containing file** for a related-code reference
 * (not just the snippet). The target line range is rendered with a left
 * accent rail and auto-scrolled into view so the reviewer can see what's
 * around it without leaving the walkthrough.
 */
export default function RelatedCodeModal({ related, onClose }: Props) {
  const { session } = useSession();
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);
  const targetLineRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    closeBtnRef.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    setContent(null);
    setError(null);
    getRepoFile(session.plan.session_id, related.anchor.file)
      .then(r => { if (!cancelled) setContent(r.content); })
      .catch(e => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [session, related.anchor.file]);

  // After content paints, scroll the highlighted region into view
  useEffect(() => {
    if (content && targetLineRef.current) {
      targetLineRef.current.scrollIntoView({ block: "center", behavior: "auto" });
    }
  }, [content]);

  const lang = languageFor(related.anchor.file);
  const [startLine, endLine] = related.anchor.line_range;

  return (
    <div className={styles.backdrop} onMouseDown={onClose} role="presentation">
      <div
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-label={`File: ${related.anchor.file}`}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className={styles.header}>
          <span className={styles.relationship}>{related.relationship}</span>
          <span className={styles.path} title={related.anchor.file}>
            {related.anchor.file}
            <span className={styles.lineRange}>
              :{startLine}{startLine !== endLine && `–${endLine}`}
            </span>
          </span>
          <button
            ref={closeBtnRef}
            className={styles.closeBtn}
            onClick={onClose}
            aria-label="Close (Esc)"
            title="Close (Esc)"
          >×</button>
        </header>
        <div className={styles.body}>
          {error && <div className={styles.errorBox}>Couldn't load file: {error}</div>}
          {!error && content === null && <div className={styles.loadingBox}>Loading file…</div>}
          {!error && content !== null && (
            <FileView
              content={content}
              lang={lang}
              startLine={startLine}
              endLine={endLine}
              targetLineRef={targetLineRef}
            />
          )}
        </div>
      </div>
    </div>
  );
}

interface FileViewProps {
  content: string;
  lang: string | null;
  startLine: number;
  endLine: number;
  targetLineRef: React.RefObject<HTMLDivElement | null>;
}

function FileView({ content, lang, startLine, endLine, targetLineRef }: FileViewProps) {
  const lines = content.split("\n");
  // Re-highlight each line individually — refractor's HAST is per-call, and
  // carving a multi-line tree by offset is error-prone for so little upside.
  const perLineHast = lines.map(l => (lang ? highlightSnippet(l || " ", lang) : null));

  return (
    <div className={styles.fileView}>
      <pre className={styles.fileCode}>
        {lines.map((line, i) => {
          const lineNo = i + 1;
          const isActive = lineNo >= startLine && lineNo <= endLine;
          const hast = perLineHast[i];
          return (
            <Fragment key={i}>
              <div
                ref={lineNo === startLine ? targetLineRef : undefined}
                className={`${styles.line} ${isActive ? styles.lineActive : ""}`}
              >
                <span className={styles.lineNumber}>{lineNo}</span>
                <span className={styles.lineText}>
                  {hast ? renderHast(hast) : line || " "}
                </span>
              </div>
            </Fragment>
          );
        })}
      </pre>
    </div>
  );
}
