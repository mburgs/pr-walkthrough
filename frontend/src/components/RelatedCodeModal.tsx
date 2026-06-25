import { useEffect, useMemo, useRef, useState } from "react";
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
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);
  const targetLineRef = useRef<HTMLDivElement>(null);

  // Keyboard close + focus trap. Capture the element that opened the
  // modal so keyboard users land back on the right row when it closes.
  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // Only honour Esc if focus is inside the dialog. Otherwise typing
        // in the follow-up textarea below the diff would also close us.
        const dialog = dialogRef.current;
        if (dialog && dialog.contains(document.activeElement)) {
          e.stopPropagation();
          onClose();
        }
      } else if (e.key === "Tab") {
        // Cycle focus within the dialog. Without this, Tab walks out into
        // the chunk list / right rail behind the backdrop.
        const dialog = dialogRef.current;
        if (!dialog) return;
        const focusables = dialog.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    closeBtnRef.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      previouslyFocused?.focus?.();
    };
  }, [onClose]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    setContent(null);
    setError(null);
    getRepoFile(session.plan.session_id, related.anchor.file)
      .then(r => { if (!cancelled) setContent(r.content); })
      .catch(e => { if (!cancelled) setError(extractDetail(e)); });
    return () => { cancelled = true; };
  }, [session, related.anchor.file]);

  const [startLine, endLine] = related.anchor.line_range;

  // Scroll the target line into view both when the content arrives and when
  // the user clicks a different related row pointing at the same file but
  // different lines (content stays cached; only startLine moves).
  useEffect(() => {
    if (content && targetLineRef.current) {
      targetLineRef.current.scrollIntoView({ block: "center", behavior: "auto" });
    }
  }, [content, startLine]);

  const lang = languageFor(related.anchor.file);

  return (
    <div className={styles.backdrop} onMouseDown={onClose} role="presentation">
      <div
        ref={dialogRef}
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
  // Tokenize once per (content, lang), then index by line. Re-highlighting
  // on every parent render (e.g. on scroll-driven re-renders) used to make
  // 1000-line files visibly janky.
  const { lines, perLineHast } = useMemo(() => {
    const split = content.split("\n");
    const hast = lang ? split.map(l => highlightSnippet(l || " ", lang)) : split.map(() => null);
    return { lines: split, perLineHast: hast };
  }, [content, lang]);

  return (
    <div className={styles.fileView}>
      <pre className={styles.fileCode}>
        {lines.map((line, i) => {
          const lineNo = i + 1;
          const isActive = lineNo >= startLine && lineNo <= endLine;
          const hast = perLineHast[i];
          return (
            <div
              key={i}
              ref={lineNo === startLine ? targetLineRef : undefined}
              className={`${styles.line} ${isActive ? styles.lineActive : ""}`}
            >
              <span className={styles.lineNumber}>{lineNo}</span>
              <span className={styles.lineText}>
                {hast ? renderHast(hast) : line || " "}
              </span>
            </div>
          );
        })}
      </pre>
    </div>
  );
}

/**
 * The backend uses FastAPI's `{detail: "..."}` envelope for error responses.
 * api/client.ts wraps the body inside `HTTP NNN: <body>` so the readable
 * message gets buried. Pull it back out for the UI.
 */
function extractDetail(err: unknown): string {
  const raw = String(err);
  const m = raw.match(/HTTP \d+: (\{.*\})/);
  if (!m) return raw;
  try {
    const parsed = JSON.parse(m[1]);
    if (parsed && typeof parsed.detail === "string") return parsed.detail;
  } catch { /* fall through */ }
  return raw;
}
