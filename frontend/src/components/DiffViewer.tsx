import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { parseDiff, Diff, Hunk as DiffHunk, Decoration, tokenize } from "react-diff-view";
import { refractor } from "refractor/all";
import type { ChunkNarration, CodeAnchor, PRMetadata, RelatedCode } from "../contracts";
import { useSession } from "../contexts/SessionContext";
import { getRepoFile } from "../api/client";
import { applyExpansion, EXPAND_LINES } from "../lib/diffExpand";

/**
 * refractor v5 returns a hast `Root` node from highlight(); react-diff-view's
 * tokenize expects the children array directly (it wraps the value in its own
 * root). Adapt by unwrapping `.children`.
 */
const refractorAdapter = {
  ...refractor,
  highlight: (value: string, language: string) => {
    const root = refractor.highlight(value, language);
    return (root as any).children ?? [];
  },
};
import "react-diff-view/style/index.css";
import type { TourChunk, Hunk } from "../contracts";
import styles from "./DiffViewer.module.css";

interface Props {
  chunk: TourChunk;
  /** Narration for this chunk — supplies related-code snippets rendered
   * as extra "reference" file cards below the main diff. Optional so the
   * diff area works while narration is still loading. */
  narration?: ChunkNarration | null;
  /** Lines to spotlight (highlight + scroll) — driven by the active narration segment. */
  activeAnchor?: CodeAnchor | null;
}

/**
 * Build a GitHub blob URL pinned to the PR's head_sha. Optionally append
 * a `#L<line>` anchor so the browser scrolls to the right spot.
 */
function githubBlobUrl(pr: PRMetadata, file: string, line?: number): string {
  const base = `https://github.com/${pr.repo}/blob/${pr.head_sha}/${file}`;
  return line != null ? `${base}#L${line}` : base;
}


/* ------------------------------------------------------------------ *
 * Language inference from file extension. Sticking to the languages
 * refractor's `all.js` bundle ships with; everything else falls back
 * to plain text (still rendered, just unhighlighted).
 * ------------------------------------------------------------------ */
function languageFor(path: string): string | null {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx", mjs: "javascript", cjs: "javascript",
    py: "python", pyi: "python",
    go: "go",
    rs: "rust",
    java: "java", kt: "kotlin", kts: "kotlin",
    rb: "ruby",
    php: "php",
    c: "c", h: "c", cpp: "cpp", cxx: "cpp", cc: "cpp", hpp: "cpp",
    cs: "csharp",
    swift: "swift",
    sh: "bash", bash: "bash", zsh: "bash", fish: "bash",
    sql: "sql",
    json: "json", yaml: "yaml", yml: "yaml", toml: "toml",
    md: "markdown", mdx: "markdown",
    html: "markup", xml: "markup", svg: "markup",
    css: "css", scss: "scss", sass: "sass", less: "less",
    dockerfile: "docker",
  };
  return map[ext] ?? null;
}

function hunksToUnifiedDiff(file: string, hunks: Hunk[]): string {
  const lines: string[] = [
    `diff --git a/${file} b/${file}`,
    `--- a/${file}`,
    `+++ b/${file}`,
  ];
  for (const h of hunks) {
    lines.push(h.header);
    lines.push(h.body);
  }
  return lines.join("\n");
}

export default function DiffViewer({ chunk, narration = null, activeAnchor = null }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const { session } = useSession();
  const pr = session?.plan.pr;
  // Per-file hunks overriding chunk.hunks once the user starts pulling more
  // context. Initialised from chunk.hunks on every chunk change so navigating
  // chunks doesn't carry stale expansions across.
  const initialFileGroups = useMemo(() => {
    const groups: Record<string, Hunk[]> = {};
    for (const hunk of chunk.hunks) {
      (groups[hunk.file] ??= []).push(hunk);
    }
    return groups;
  }, [chunk]);
  const [hunksByFile, setHunksByFile] = useState<Record<string, Hunk[]>>(initialFileGroups);
  // Cache of fetched file contents (lines) per file path. Survives across
  // expand clicks on the same file; reset on chunk change.
  const [fileLines, setFileLines] = useState<Record<string, string[]>>({});
  const [expandingFile, setExpandingFile] = useState<string | null>(null);
  const fileLinesRef = useRef(fileLines);
  useEffect(() => { fileLinesRef.current = fileLines; }, [fileLines]);

  useEffect(() => {
    setHunksByFile(initialFileGroups);
    setFileLines({});
  }, [initialFileGroups]);

  const fileGroups = hunksByFile;

  const ensureFileLines = useCallback(async (file: string): Promise<string[] | null> => {
    const cached = fileLinesRef.current[file];
    if (cached) return cached;
    if (!session) return null;
    try {
      const res = await getRepoFile(session.plan.session_id, file);
      // Strip a single trailing newline so the line count matches the file's
      // actual content (Unix files commonly end with \n; splitting on "\n"
      // would otherwise produce a phantom empty last line that throws off
      // the file-boundary cap in expand-down).
      const text = res.content.endsWith("\n") ? res.content.slice(0, -1) : res.content;
      const lines = text.split("\n");
      setFileLines((prev) => ({ ...prev, [file]: lines }));
      return lines;
    } catch (e) {
      console.warn("expand-context fetch failed for", file, e);
      return null;
    }
  }, [session]);

  const expand = useCallback(async (file: string, idx: number, direction: "up" | "down") => {
    if (expandingFile) return;
    setExpandingFile(file);
    try {
      const lines = await ensureFileLines(file);
      if (!lines) return;
      setHunksByFile((prev) => {
        const current = prev[file] ?? [];
        return { ...prev, [file]: applyExpansion(current, idx, direction, lines, EXPAND_LINES) };
      });
    } finally {
      setExpandingFile(null);
    }
  }, [ensureFileLines, expandingFile]);

  // Highlight + scroll the rows matching activeAnchor's new-side line range.
  // react-diff-view tags each tr with .diff-line and uses the second gutter td
  // for the new-side line number; we read that to identify rows.
  useEffect(() => {
    const root = containerRef.current;
    if (!root) return;
    // Clear previous highlight
    root.querySelectorAll(`.${styles.activeRow}`).forEach(el => el.classList.remove(styles.activeRow));
    if (!activeAnchor) return;

    const fileCard = root.querySelector<HTMLElement>(`[data-file="${cssEscape(activeAnchor.file)}"]`);
    if (!fileCard) return;
    const [start, end] = activeAnchor.line_range;

    const matched: HTMLElement[] = [];
    fileCard.querySelectorAll<HTMLTableRowElement>("tr.diff-line").forEach(tr => {
      const ln = readNewSideLine(tr);
      if (ln != null && ln >= start && ln <= end) {
        tr.classList.add(styles.activeRow);
        matched.push(tr);
      }
    });

    if (matched.length === 0) return;
    // Scroll the first matched line into view (smooth)
    matched[0].scrollIntoView({ behavior: "smooth", block: "center" });
  }, [activeAnchor, chunk, hunksByFile]);

  if (chunk.hunks.length === 0) {
    return <div className={styles.noHunks}>No diff for this chunk.</div>;
  }

  return (
    <div className={styles.container} ref={containerRef}>
      {Object.entries(fileGroups).map(([file, hunks]) => {
        const lang = languageFor(file);
        const diffText = hunksToUnifiedDiff(file, hunks);
        let parsedFiles: ReturnType<typeof parseDiff> = [];
        try {
          parsedFiles = parseDiff(diffText);
        } catch {
          return <FallbackPre key={file} file={file} hunks={hunks} />;
        }

        return parsedFiles.map((parsed, i) => {
          // Syntax-highlight tokens via refractor (Prism in tree form)
          let tokens: ReturnType<typeof tokenize> | undefined;
          if (lang) {
            try {
              tokens = tokenize(parsed.hunks, {
                refractor: refractorAdapter as any,
                language: lang,
                highlight: true,
              });
            } catch (e) {
              console.warn("syntax highlight failed for", lang, e);
              tokens = undefined;
            }
          }

          const filePath = parsed.newPath ?? parsed.oldPath ?? "unknown";
          return (
            <div key={`${filePath}-${i}`} className={styles.file} data-file={filePath}>
              <div className={styles.fileHeader}>
                <span className={styles.fileIcon} aria-hidden>◰</span>
                {pr ? (
                  <a
                    className={styles.fileName}
                    href={githubBlobUrl(pr, filePath)}
                    target="_blank"
                    rel="noreferrer"
                    title={`Open ${filePath} on GitHub @ ${pr.head_sha.slice(0, 7)}`}
                  >{filePath}</a>
                ) : (
                  <span className={styles.fileName}>{filePath}</span>
                )}
                <span className={styles.fileType}>{parsed.type}</span>
                {lang && <span className={styles.langBadge}>{lang}</span>}
              </div>
              <div className={styles.diffWrap}>
                <Diff
                  viewType="unified"
                  diffType={parsed.type}
                  hunks={parsed.hunks}
                  tokens={tokens}
                  className={styles.diffTable}
                >
                  {(hs) => hs.flatMap((hunk, idx) => {
                    // Decoration above every hunk (gap indicator + expand
                    // controls); plus a trailing decoration after the last
                    // hunk so the user can pull more context off the bottom
                    // of the file.
                    const prev = idx > 0 ? hs[idx - 1] : null;
                    const skipped = prev
                      ? hunk.newStart - (prev.newStart + prev.newLines)
                      : 0;
                    const label = prev
                      ? `${skipped} line${skipped === 1 ? "" : "s"} hidden`
                      : `@@ -${hunk.oldStart},${hunk.oldLines} +${hunk.newStart},${hunk.newLines} @@`;
                    const showUp = hunk.newStart > 1;
                    const busy = expandingFile === filePath;
                    // Hide the trailing ▼ once we know the file length and
                    // the last hunk already reaches the end. Before the file
                    // is fetched we keep the button optimistically; the
                    // first click pulls the lines and a re-render hides it.
                    const knownLines = fileLines[filePath];
                    const lastHunkNewEnd = hunk.newStart + hunk.newLines - 1;
                    const showTailDown =
                      !knownLines || lastHunkNewEnd < knownLines.length;
                    // GitHub-style: arrows live in the gutter cell (where
                    // line numbers normally sit) and the @@-style label
                    // takes the content cell. <Decoration> wires children
                    // as [gutter, content] when count === 2.
                    const nodes = [
                      <Decoration key={`dec-${hunk.content}`}>
                        <span className={styles.gutterArrows}>
                          {prev && (
                            <button
                              type="button"
                              className={styles.gutterArrowBtn}
                              disabled={busy}
                              onClick={() => expand(filePath, idx - 1, "down")}
                              title={`Expand ${EXPAND_LINES} lines down`}
                              aria-label="Expand context down on previous section"
                            >↓</button>
                          )}
                          {showUp && (
                            <button
                              type="button"
                              className={styles.gutterArrowBtn}
                              disabled={busy}
                              onClick={() => expand(filePath, idx, "up")}
                              title={`Expand ${EXPAND_LINES} lines up`}
                              aria-label="Expand context up"
                            >↑</button>
                          )}
                        </span>
                        <span className={styles.hunkGapLabel}>{label}</span>
                      </Decoration>,
                      <DiffHunk key={hunk.content} hunk={hunk} />,
                    ];
                    if (idx === hs.length - 1 && showTailDown) {
                      nodes.push(
                        <Decoration key={`dec-tail-${hunk.content}`}>
                          <span className={styles.gutterArrows}>
                            <button
                              type="button"
                              className={styles.gutterArrowBtn}
                              disabled={busy}
                              onClick={() => expand(filePath, idx, "down")}
                              title={`Expand ${EXPAND_LINES} lines down`}
                              aria-label="Expand context down"
                            >↓</button>
                          </span>
                          <span className={styles.hunkGapLabel} />
                        </Decoration>
                      );
                    }
                    return nodes;
                  })}
                </Diff>
              </div>
            </div>
          );
        });
      })}

      {narration && narration.related_code.length > 0 && (
        <div className={styles.referenceGroup}>
          <div className={styles.referenceGroupHeader}>
            Referenced code — pulled in for context, not part of this PR
          </div>
          {narration.related_code.map((r, i) => (
            <ReferenceFileCard
              key={`ref-${i}-${r.anchor.file}-${r.anchor.line_range[0]}`}
              related={r}
              pr={pr ?? null}
              sessionId={session?.plan.session_id ?? null}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Build the initial Hunk for a reference snippet. All lines are
 * context (no +/-), so both old and new ranges match the snippet's
 * start/count. Kept in Hunk shape so we can reuse `applyExpansion`
 * for the ▲/▼ arrows without a separate code path.
 */
function snippetToHunk(file: string, startLine: number, snippet: string): Hunk {
  const bodyLines = snippet.split("\n");
  const count = bodyLines.length;
  return {
    file,
    old_range: [startLine, count],
    new_range: [startLine, count],
    header: `@@ -${startLine},${count} +${startLine},${count} @@`,
    body: bodyLines.map((l) => " " + l).join("\n"),
  };
}

function ReferenceFileCard({
  related,
  pr,
  sessionId,
}: {
  related: RelatedCode;
  pr: PRMetadata | null;
  sessionId: string | null;
}) {
  const { anchor, relationship, snippet, target_line } = related;
  const lang = languageFor(anchor.file);
  const cardRef = useRef<HTMLDivElement>(null);
  const [hunks, setHunks] = useState<Hunk[]>(() =>
    [snippetToHunk(anchor.file, anchor.line_range[0], snippet)]
  );
  const [fileLines, setFileLines] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);

  // Reset when the related item identity changes (new narration).
  useEffect(() => {
    setHunks([snippetToHunk(anchor.file, anchor.line_range[0], snippet)]);
    setFileLines(null);
  }, [anchor.file, anchor.line_range, snippet]);

  const ensureLines = useCallback(async (): Promise<string[] | null> => {
    if (fileLines) return fileLines;
    if (!sessionId) return null;
    try {
      const res = await getRepoFile(sessionId, anchor.file);
      const text = res.content.endsWith("\n") ? res.content.slice(0, -1) : res.content;
      const lines = text.split("\n");
      setFileLines(lines);
      return lines;
    } catch (e) {
      console.warn("reference expand fetch failed for", anchor.file, e);
      return null;
    }
  }, [fileLines, sessionId, anchor.file]);

  const expand = useCallback(async (direction: "up" | "down") => {
    if (busy) return;
    setBusy(true);
    try {
      const lines = await ensureLines();
      if (!lines) return;
      setHunks((prev) => applyExpansion(prev, prev.length - 1, direction, lines, EXPAND_LINES));
    } finally {
      setBusy(false);
    }
  }, [busy, ensureLines]);

  // Paint the retriever's target line with a subtle backlight so the
  // reviewer sees which row was the match without losing the
  // surrounding context. Uses the new-side gutter text to find the row.
  useEffect(() => {
    const root = cardRef.current;
    if (!root || target_line == null) return;
    root.querySelectorAll(`.${styles.targetRow}`).forEach(el => el.classList.remove(styles.targetRow));
    root.querySelectorAll<HTMLTableRowElement>("tr.diff-line").forEach(tr => {
      const ln = readNewSideLine(tr);
      if (ln === target_line) tr.classList.add(styles.targetRow);
    });
  }, [target_line, hunks]);

  const diffText = hunksToUnifiedDiff(anchor.file, hunks);
  let parsedFiles: ReturnType<typeof parseDiff> = [];
  try {
    parsedFiles = parseDiff(diffText);
  } catch {
    return <FallbackReference anchor={anchor} snippet={snippet} relationship={relationship} />;
  }
  const parsed = parsedFiles[0];
  if (!parsed) {
    return <FallbackReference anchor={anchor} snippet={snippet} relationship={relationship} />;
  }
  let tokens: ReturnType<typeof tokenize> | undefined;
  if (lang) {
    try {
      tokens = tokenize(parsed.hunks, {
        refractor: refractorAdapter as any,
        language: lang,
        highlight: true,
      });
    } catch (e) {
      console.warn("syntax highlight failed for reference snippet", lang, e);
    }
  }

  const currentHunk = hunks[0];
  const rangeStart = currentHunk.new_range[0];
  const rangeEnd = rangeStart + currentHunk.new_range[1] - 1;
  const canUp = rangeStart > 1;
  const canDown = !fileLines || rangeEnd < fileLines.length;

  return (
    <div ref={cardRef} className={`${styles.file} ${styles.referenceFile}`}>
      <div className={styles.fileHeader}>
        <span className={styles.referenceBadge} title="Pulled in for context — not part of this PR">
          REFERENCE
        </span>
        <span className={styles.fileIcon} aria-hidden>◰</span>
        {pr ? (
          <a
            className={styles.fileName}
            href={githubBlobUrl(pr, anchor.file, target_line ?? anchor.line_range[0])}
            target="_blank"
            rel="noreferrer"
            title={`Open ${anchor.file} on GitHub @ ${pr.head_sha.slice(0, 7)}`}
          >{anchor.file}</a>
        ) : (
          <span className={styles.fileName}>{anchor.file}</span>
        )}
        <span className={styles.relationshipBadge}>{relationship}</span>
        {lang && <span className={styles.langBadge}>{lang}</span>}
      </div>
      <div className={styles.diffWrap}>
        <Diff
          viewType="unified"
          diffType="modify"
          hunks={parsed.hunks}
          tokens={tokens}
          className={styles.diffTable}
        >
          {(hs) => hs.flatMap((hunk, idx) => {
            const nodes: React.ReactElement[] = [
              <Decoration key={`ref-dec-up-${hunk.content}`}>
                <span className={styles.gutterArrows}>
                  {canUp && (
                    <button
                      type="button"
                      className={styles.gutterArrowBtn}
                      disabled={busy}
                      onClick={() => expand("up")}
                      title={`Expand ${EXPAND_LINES} lines up`}
                      aria-label="Expand context up"
                    >↑</button>
                  )}
                </span>
                <span className={styles.hunkGapLabel} />
              </Decoration>,
              <DiffHunk key={hunk.content} hunk={hunk} />,
            ];
            if (idx === hs.length - 1 && canDown) {
              nodes.push(
                <Decoration key={`ref-dec-down-${hunk.content}`}>
                  <span className={styles.gutterArrows}>
                    <button
                      type="button"
                      className={styles.gutterArrowBtn}
                      disabled={busy}
                      onClick={() => expand("down")}
                      title={`Expand ${EXPAND_LINES} lines down`}
                      aria-label="Expand context down"
                    >↓</button>
                  </span>
                  <span className={styles.hunkGapLabel} />
                </Decoration>
              );
            }
            return nodes;
          })}
        </Diff>
      </div>
    </div>
  );
}

function FallbackReference({
  anchor, snippet, relationship,
}: {
  anchor: CodeAnchor;
  snippet: string;
  relationship: string;
}) {
  return (
    <div className={`${styles.file} ${styles.referenceFile}`}>
      <div className={styles.fileHeader}>
        <span className={styles.referenceBadge}>REFERENCE</span>
        <span className={styles.fileName}>{anchor.file}</span>
        <span className={styles.relationshipBadge}>{relationship}</span>
      </div>
      <pre className={styles.fallback}>{snippet}</pre>
    </div>
  );
}

/**
 * Read the new-side line number off a diff row.
 *
 * In react-diff-view's unified view each tr has two `.diff-gutter` cells:
 * old-side then new-side. The new-side cell shows the line number for
 * insert/normal rows; deletes show old-only. We grab the LAST gutter td
 * (always the new side) and parse its text.
 */
function readNewSideLine(tr: HTMLTableRowElement): number | null {
  const gutters = tr.querySelectorAll<HTMLTableCellElement>("td.diff-gutter");
  const newSide = gutters[gutters.length - 1];
  if (!newSide) return null;
  const n = parseInt(newSide.textContent?.trim() || "", 10);
  return Number.isFinite(n) ? n : null;
}

function cssEscape(value: string): string {
  if (typeof window !== "undefined" && (window as any).CSS?.escape) {
    return (window as any).CSS.escape(value);
  }
  return value.replace(/["\\]/g, "\\$&");
}

function FallbackPre({ file, hunks }: { file: string; hunks: Hunk[] }) {
  return (
    <div className={styles.file}>
      <div className={styles.fileHeader}>
        <span className={styles.fileIcon} aria-hidden>◰</span>
        <span className={styles.fileName}>{file}</span>
      </div>
      <pre className={styles.fallback}>
        {hunks.map((h) => h.header + "\n" + h.body).join("\n")}
      </pre>
    </div>
  );
}
