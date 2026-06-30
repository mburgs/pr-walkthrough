import type { Hunk } from "../contracts";

export const EXPAND_LINES = 10;

function buildHeader(
  oldStart: number,
  oldCount: number,
  newStart: number,
  newCount: number,
  ctx: string,
): string {
  const fmtOld = oldCount === 1 ? `${oldStart}` : `${oldStart},${oldCount}`;
  const fmtNew = newCount === 1 ? `${newStart}` : `${newStart},${newCount}`;
  const suffix = ctx ? ` ${ctx}` : "";
  return `@@ -${fmtOld} +${fmtNew} @@${suffix}`;
}

function contextOfHeader(header: string): string {
  const m = header.match(/^@@\s+[^@]+@@\s*(.*)$/);
  return m ? m[1].trim() : "";
}

function joinContextLines(fileLines: string[], start: number, end: number): string {
  if (end < start) return "";
  return fileLines
    .slice(start - 1, end)
    .map((l) => " " + l)
    .join("\n");
}

export function expandHunkUp(
  hunk: Hunk,
  fileLines: string[],
  n: number,
  minNewStart: number,
): Hunk {
  const [oldStart, oldCount] = hunk.old_range;
  const [newStart, newCount] = hunk.new_range;
  const desired = Math.min(n, newStart - minNewStart);
  if (desired <= 0) return hunk;
  const added = joinContextLines(fileLines, newStart - desired, newStart - 1);
  const sep = hunk.body && added ? "\n" : "";
  return {
    ...hunk,
    old_range: [Math.max(1, oldStart - desired), oldCount + desired],
    new_range: [newStart - desired, newCount + desired],
    header: buildHeader(
      Math.max(1, oldStart - desired),
      oldCount + desired,
      newStart - desired,
      newCount + desired,
      contextOfHeader(hunk.header),
    ),
    body: added + sep + hunk.body,
  };
}

export function expandHunkDown(
  hunk: Hunk,
  fileLines: string[],
  n: number,
  maxNewEnd: number,
): Hunk {
  const [oldStart, oldCount] = hunk.old_range;
  const [newStart, newCount] = hunk.new_range;
  const currentNewEnd = newStart + newCount - 1;
  const desired = Math.min(n, maxNewEnd - currentNewEnd);
  if (desired <= 0) return hunk;
  const added = joinContextLines(fileLines, currentNewEnd + 1, currentNewEnd + desired);
  const sep = hunk.body && added ? "\n" : "";
  return {
    ...hunk,
    old_range: [oldStart, oldCount + desired],
    new_range: [newStart, newCount + desired],
    header: buildHeader(
      oldStart,
      oldCount + desired,
      newStart,
      newCount + desired,
      contextOfHeader(hunk.header),
    ),
    body: hunk.body + sep + added,
  };
}

/**
 * Merge two adjacent hunks if their new-side ranges touch or overlap.
 * Returns null when there's still a gap.
 *
 * The overlap case shows up after expand-context grows hunk A's bottom past
 * hunk B's start: A's appended context lines duplicate B's leading context.
 * We drop the duplicated leading context off the front of B's body so the
 * merged hunk doesn't render the shared lines twice.
 */
export function mergeIfTouching(a: Hunk, b: Hunk): Hunk | null {
  const aNewStart = a.new_range[0];
  const aNewCount = a.new_range[1];
  const aNewEnd = aNewStart + aNewCount - 1;
  const bNewStart = b.new_range[0];
  const gap = bNewStart - (aNewEnd + 1);
  if (gap > 0) return null;
  const overlap = -gap;
  let bBody = b.body;
  let droppedLines = 0;
  if (overlap > 0) {
    const bLines = bBody.split("\n");
    let i = 0;
    while (i < bLines.length && droppedLines < overlap && bLines[i].startsWith(" ")) {
      droppedLines++;
      i++;
    }
    bBody = bLines.slice(i).join("\n");
  }
  const mergedNewCount = bNewStart + b.new_range[1] - aNewStart - droppedLines;
  const mergedOldCount = b.old_range[0] + b.old_range[1] - a.old_range[0] - droppedLines;
  const sep = a.body && bBody ? "\n" : "";
  return {
    file: a.file,
    old_range: [a.old_range[0], mergedOldCount],
    new_range: [aNewStart, mergedNewCount],
    header: buildHeader(
      a.old_range[0],
      mergedOldCount,
      aNewStart,
      mergedNewCount,
      contextOfHeader(a.header),
    ),
    body: a.body + sep + bBody,
  };
}

export function applyExpansion(
  hunks: Hunk[],
  idx: number,
  direction: "up" | "down",
  fileLines: string[],
  n: number = EXPAND_LINES,
): Hunk[] {
  if (idx < 0 || idx >= hunks.length) return hunks;
  const next = [...hunks];
  const prev = idx > 0 ? next[idx - 1] : null;
  const after = idx < next.length - 1 ? next[idx + 1] : null;
  const minNewStart = prev ? prev.new_range[0] + prev.new_range[1] : 1;
  const maxNewEnd = after ? after.new_range[0] - 1 : fileLines.length;
  next[idx] =
    direction === "up"
      ? expandHunkUp(next[idx], fileLines, n, minNewStart)
      : expandHunkDown(next[idx], fileLines, n, maxNewEnd);
  if (direction === "up" && idx > 0) {
    const merged = mergeIfTouching(next[idx - 1], next[idx]);
    if (merged) next.splice(idx - 1, 2, merged);
  } else if (direction === "down" && idx < next.length - 1) {
    const merged = mergeIfTouching(next[idx], next[idx + 1]);
    if (merged) next.splice(idx, 2, merged);
  }
  return next;
}
