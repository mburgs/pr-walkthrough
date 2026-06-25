import { useState, useMemo, useEffect, useRef } from "react";
import type {
  ChunkNarration,
  CodeAnchor,
  Concern,
  Flag,
  TourChunk,
} from "../contracts";
import { useSession } from "../contexts/SessionContext";
import NarrationPlayer from "./NarrationPlayer";
import { highlightSnippet, languageFor, renderHast } from "../lib/highlight";
import styles from "./RightRail.module.css";

interface Props {
  chunk: TourChunk | null;
  narration: ChunkNarration | null;
  narrationLoading: boolean;
  collapsed: boolean;
  onToggle: () => void;
  onSegmentChange?: (segmentIndex: number) => void;
  /** Clicked anchor — drives the diff highlight + scroll. */
  onAnchorClick?: (anchor: CodeAnchor | null) => void;
  /** Currently-highlighted anchor (to mark the matching row). */
  activeAnchor?: CodeAnchor | null;
}

function anchorEq(a: CodeAnchor | null | undefined, b: CodeAnchor | null | undefined): boolean {
  if (!a || !b) return false;
  return a.file === b.file && a.line_range[0] === b.line_range[0] && a.line_range[1] === b.line_range[1];
}

/**
 * Right rail: unified accordion stack — narration player + highlights,
 * concerns, related, look-closer, and the Flags tracker — all in one
 * scroll. Each section shows its count and is auto-collapsed when empty.
 * The whole rail collapses to a thin icon strip for diff-focused work.
 */
export default function RightRail({
  chunk,
  narration,
  narrationLoading,
  collapsed,
  onToggle,
  onSegmentChange,
  onAnchorClick,
  activeAnchor,
}: Props) {
  const { flags } = useSession();

  const sectionCounts = useMemo(() => ({
    concerns:   narration?.concerns.length ?? 0,
    related:    narration?.related_code.length ?? 0,
    look:       narration?.look_closer_for.length ?? 0,
    flags:      flags.length,
  }), [narration, flags]);

  if (collapsed) {
    const totalConcerns = sectionCounts.concerns;
    return (
      <div className={styles.collapsedRail}>
        <button
          className={styles.collapseToggle}
          onClick={onToggle}
          title="Expand insights panel"
          aria-label="Expand insights panel"
        >‹</button>
        <div className={styles.collapsedStack}>
          <CollapsedDot label="Concerns" count={totalConcerns} variant="warn" />
          <CollapsedDot label="Related" count={sectionCounts.related} variant="muted" />
          <CollapsedDot label="Look closer" count={sectionCounts.look} variant="muted" />
          <CollapsedDot label="Flags" count={sectionCounts.flags} variant="accent" />
        </div>
      </div>
    );
  }

  return (
    <div className={styles.rail}>
      <div className={styles.railHeader}>
        <span className={styles.railLabel}>
          {chunk ? <><span className={styles.railChunkId}>{chunk.chunk_id}</span> · {chunk.files.length} file{chunk.files.length === 1 ? "" : "s"}</> : "—"}
        </span>
        <button
          className={styles.collapseToggle}
          onClick={onToggle}
          title="Collapse insights panel"
          aria-label="Collapse insights panel"
        >›</button>
      </div>

      <div className={styles.scroll}>
        {/* Narration section sits at the top, always expanded */}
        <div className={styles.narrationBlock}>
          {chunk && (
            <NarrationPlayer
              chunk={chunk}
              narration={narration}
              loading={narrationLoading}
              onSegmentChange={onSegmentChange}
            />
          )}
        </div>

        <Section
          title="Concerns"
          count={sectionCounts.concerns}
          severity={highestSeverity(narration?.concerns ?? [])}
          defaultOpen={sectionCounts.concerns > 0}
        >
          {narration?.concerns.map((c, i) => (
            <ConcernRow
              key={i}
              concern={c}
              chunkId={narration.chunk_id}
              activeAnchor={activeAnchor}
              onAnchorClick={onAnchorClick}
            />
          ))}
        </Section>

        <Section
          title="Related"
          count={sectionCounts.related}
          defaultOpen={false}
        >
          {narration?.related_code.map((r, i) => {
            const lang = languageFor(r.anchor.file);
            const hast = highlightSnippet(r.snippet, lang);
            return (
              <div key={i} className={styles.row}>
                <div className={styles.rowHeader}>
                  <span className={styles.relationship}>{r.relationship}</span>
                  <Anchor file={r.anchor.file} line={r.anchor.line_range} />
                </div>
                <pre className={styles.snippet}>
                  {hast ? renderHast(hast) : r.snippet}
                </pre>
              </div>
            );
          })}
        </Section>

        <Section
          title="Look closer"
          count={sectionCounts.look}
          defaultOpen={false}
        >
          {narration?.look_closer_for.map((item, i) => (
            <div key={i} className={styles.bullet}>{item}</div>
          ))}
        </Section>

        <Section
          title="Flags"
          count={sectionCounts.flags}
          defaultOpen={sectionCounts.flags > 0}
          accent={sectionCounts.flags > 0}
        >
          {flags.length === 0 ? (
            <div className={styles.emptyHint}>Add concerns to the flag list from above, then post to GitHub.</div>
          ) : (
            flags.map((f) => <FlagRow key={f.flag_id} flag={f} />)
          )}
        </Section>
      </div>
    </div>
  );
}

/* ---------- Section ---------- */

interface SectionProps {
  title: string;
  count: number;
  defaultOpen?: boolean;
  severity?: "low" | "medium" | "high" | null;
  accent?: boolean;
  children: React.ReactNode;
}

function Section({ title, count, defaultOpen = false, severity, accent, children }: SectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  // Sync open state when defaultOpen flips (e.g. when narration arrives and
  // populates a section that started empty). Once the user toggles manually
  // they take over: subsequent prop changes are ignored.
  const userTouched = useRef(false);
  useEffect(() => {
    if (!userTouched.current) setOpen(defaultOpen);
  }, [defaultOpen]);
  const isEmpty = count === 0;
  return (
    <div className={`${styles.section} ${open ? styles.sectionOpen : ""}`}>
      <button
        className={styles.sectionHeader}
        onClick={() => { if (!isEmpty) { userTouched.current = true; setOpen((v) => !v); } }}
        disabled={isEmpty}
      >
        <span className={styles.chevron} aria-hidden>{open ? "▾" : "▸"}</span>
        <span className={styles.sectionTitle}>{title}</span>
        {count > 0 ? (
          <span
            className={`${styles.count} ${severity ? styles[`count_${severity}`] : ""} ${accent ? styles.count_accent : ""}`}
          >{count}</span>
        ) : (
          <span className={styles.countMuted}>0</span>
        )}
      </button>
      {open && !isEmpty && <div className={styles.sectionBody}>{children}</div>}
    </div>
  );
}

/* ---------- Concern row (with "Add to flags" action) ---------- */

function ConcernRow({
  concern,
  chunkId,
  activeAnchor,
  onAnchorClick,
}: {
  concern: Concern;
  chunkId: string;
  activeAnchor?: CodeAnchor | null;
  onAnchorClick?: (anchor: CodeAnchor | null) => void;
}) {
  const { addFlag } = useSession();
  const [added, setAdded] = useState(false);

  const handleAdd = async () => {
    await addFlag({
      chunk_id: chunkId,
      anchor: concern.anchor,
      severity: concern.severity,
      body: concern.suggested_question,
    });
    setAdded(true);
  };

  return (
    <ClickableRow
      anchor={concern.anchor ?? null}
      activeAnchor={activeAnchor}
      onClick={() => concern.anchor && onAnchorClick?.(concern.anchor)}
    >
      <div className={styles.rowHeader}>
        <SeverityBadge severity={concern.severity} />
        {concern.anchor && <Anchor file={concern.anchor.file} line={concern.anchor.line_range} />}
      </div>
      <div className={styles.rowText}>{concern.text}</div>
      {concern.suggested_question && (
        <div className={styles.quotedQ}>{concern.suggested_question}</div>
      )}
      {added ? (
        <span className={styles.addedNote}>✓ flagged</span>
      ) : (
        <button
          className={styles.miniBtn}
          onClick={(e) => { e.stopPropagation(); handleAdd(); }}
        >+ Flag</button>
      )}
    </ClickableRow>
  );
}

/* ---------- Flag row ---------- */

function FlagRow({ flag }: { flag: Flag }) {
  const { updateFlag, postFlag, deleteFlag } = useSession();
  const [body, setBody] = useState(flag.body);
  const [posting, setPosting] = useState(false);
  const [posted, setPosted] = useState(flag.posted);
  const [postedUrl, setPostedUrl] = useState(flag.posted_url);

  const handleBlur = async () => {
    if (body !== flag.body) await updateFlag(flag.flag_id, { body });
  };

  const handlePost = async () => {
    setPosting(true);
    try {
      const updated = await postFlag(flag.flag_id);
      setPosted(updated.posted);
      setPostedUrl(updated.posted_url);
    } finally {
      setPosting(false);
    }
  };

  return (
    <div className={styles.row}>
      <div className={styles.rowHeader}>
        <SeverityBadge severity={flag.severity} />
        {flag.anchor && <Anchor file={flag.anchor.file} line={flag.anchor.line_range} />}
      </div>
      <textarea
        className={styles.flagBody}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        onBlur={handleBlur}
        disabled={posted}
        rows={3}
      />
      <div className={styles.rowActions}>
        {!posted ? (
          <button className={styles.primaryBtn} onClick={handlePost} disabled={posting}>
            {posting ? "Posting…" : "Post to PR"}
          </button>
        ) : (
          <span className={styles.postedNote}>✓ posted</span>
        )}
        <button className={styles.subtleBtn} onClick={() => deleteFlag(flag.flag_id)}>Remove</button>
        {postedUrl && (
          <a className={styles.link} href={postedUrl} target="_blank" rel="noreferrer">
            View →
          </a>
        )}
      </div>
    </div>
  );
}

/* ---------- Atoms ---------- */

function ClickableRow({
  anchor, activeAnchor, onClick, children,
}: {
  anchor: CodeAnchor | null;
  activeAnchor?: CodeAnchor | null;
  onClick?: () => void;
  children: React.ReactNode;
}) {
  const active = anchor ? anchorEq(anchor, activeAnchor) : false;
  const interactive = !!anchor && !!onClick;
  return (
    <div
      className={`${styles.row} ${interactive ? styles.clickable : ""} ${active ? styles.rowActive : ""}`}
      onClick={interactive ? onClick : undefined}
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      onKeyDown={interactive ? (e) => { if (e.key === "Enter") onClick?.(); } : undefined}
      title={interactive ? "Click to highlight in diff" : undefined}
    >
      {children}
    </div>
  );
}

function Anchor({ file, line }: { file: string; line: [number, number] }) {
  const short = file.split("/").slice(-2).join("/");
  return (
    <span className={styles.anchor} title={file}>
      {short}<span className={styles.anchorLine}>:{line[0]}{line[0] !== line[1] && `–${line[1]}`}</span>
    </span>
  );
}

function SeverityBadge({ severity }: { severity: "low" | "medium" | "high" }) {
  return <span className={`${styles.sev} ${styles[`sev_${severity}`]}`}>{severity}</span>;
}

function CollapsedDot({ label, count, variant }: {
  label: string; count: number; variant: "info" | "warn" | "muted" | "accent";
}) {
  return (
    <div
      className={`${styles.collapsedDot} ${styles[`dot_${variant}`]} ${count === 0 ? styles.dotEmpty : ""}`}
      title={`${label}: ${count}`}
    >
      <span className={styles.dotLabel}>{label[0]}</span>
      {count > 0 && <span className={styles.dotCount}>{count}</span>}
    </div>
  );
}

function highestSeverity(concerns: Concern[]): "low" | "medium" | "high" | null {
  if (concerns.some((c) => c.severity === "high")) return "high";
  if (concerns.some((c) => c.severity === "medium")) return "medium";
  if (concerns.some((c) => c.severity === "low")) return "low";
  return null;
}
