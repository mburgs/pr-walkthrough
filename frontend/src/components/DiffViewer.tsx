import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { parseDiff, Diff, Hunk as DiffHunk, Decoration, tokenize } from "react-diff-view";
import { refractor } from "refractor/all";
import type { CodeAnchor } from "../contracts";
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
  /** Lines to spotlight (highlight + scroll) — driven by the active narration segment. */
  activeAnchor?: CodeAnchor | null;
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

export default function DiffViewer({ chunk, activeAnchor = null }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const { session } = useSession();
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
                <span className={styles.fileName}>{filePath}</span>
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
