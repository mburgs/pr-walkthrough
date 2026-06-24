import { useMemo } from "react";
import { parseDiff, Diff, Hunk as DiffHunk } from "react-diff-view";
import "react-diff-view/style/index.css";
import type { TourChunk, ChunkNarration, Hunk } from "../contracts";
import styles from "./DiffViewer.module.css";

interface Props {
  chunk: TourChunk;
  narration: ChunkNarration | null;
}

/**
 * Convert our Hunk contract objects into a unified diff string that parseDiff can consume.
 */
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
  // Group hunks by file
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
        const diffText = hunksToUnifiedDiff(file, hunks);
        let files: ReturnType<typeof parseDiff> = [];
        try {
          files = parseDiff(diffText);
        } catch {
          // Fallback: show raw body
          return (
            <div key={file}>
              <div className={styles.fileHeader}>
                <span className={styles.fileName}>{file}</span>
              </div>
              <pre style={{ padding: 12, fontSize: 12, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: "0 0 var(--radius) var(--radius)" }}>
                {hunks.map((h) => h.header + "\n" + h.body).join("\n")}
              </pre>
            </div>
          );
        }

        return files.map((file, i) => (
          <div key={`${file.newPath ?? i}`}>
            <div className={styles.fileHeader}>
              <span className={styles.fileName}>{file.newPath ?? file.oldPath ?? "unknown"}</span>
              <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: "auto" }}>
                {file.type}
              </span>
            </div>
            <table className={styles.diffTable}>
              <tbody>
                <Diff viewType="unified" diffType={file.type} hunks={file.hunks}>
                  {(hs) => hs.map((hunk) => <DiffHunk key={hunk.content} hunk={hunk} />)}
                </Diff>
              </tbody>
            </table>
          </div>
        ));
      })}
    </div>
  );
}
