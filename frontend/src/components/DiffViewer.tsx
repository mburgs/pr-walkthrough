import { useMemo } from "react";
import { parseDiff, Diff, Hunk as DiffHunk, tokenize } from "react-diff-view";
import { refractor } from "refractor/all";

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

export default function DiffViewer({ chunk }: Props) {
  const fileGroups = useMemo(() => {
    const groups: Record<string, Hunk[]> = {};
    for (const hunk of chunk.hunks) {
      (groups[hunk.file] ??= []).push(hunk);
    }
    return groups;
  }, [chunk]);

  if (chunk.hunks.length === 0) {
    return <div className={styles.noHunks}>No diff hunks for this chunk.</div>;
  }

  return (
    <div className={styles.container}>
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

          return (
            <div key={`${parsed.newPath ?? i}`} className={styles.file}>
              <div className={styles.fileHeader}>
                <span className={styles.fileIcon} aria-hidden>◰</span>
                <span className={styles.fileName}>{parsed.newPath ?? parsed.oldPath ?? "unknown"}</span>
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
                  {(hs) => hs.map((hunk) => (
                    <DiffHunk key={hunk.content} hunk={hunk} />
                  ))}
                </Diff>
              </div>
            </div>
          );
        });
      })}
    </div>
  );
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
