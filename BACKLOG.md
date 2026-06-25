# Backlog

Captured during dev iteration. Each item belongs to a future PR; not blockers
on current work.

## Tour-shaped chunk ordering

The current planner groups by file and orders chunks largely top-to-bottom.
A real walkthrough orders by *narrative*: entry point → core mechanism →
callers/wiring → tests → migrations. Reference: see the WHAT TO SAY priorities
in `backend/pr_walkthrough/llm/STYLE_NOTES.md`.

Specifics:

- Allow a file's hunks to be split across multiple chunks (e.g. the public
  API of a file shown early; its internals shown later near callers).
- Allow the same hunk(s) to appear in more than one chunk where doing so
  serves the tour (a util's definition shown both with its definition file
  AND with the chunk where it's first used).
- Optional lightweight **grouping** in the sidebar — a label or a thin
  divider between groups ("API surface", "Model / service", "Tests").
  Keep it visually quiet so it doesn't compete with the chunk list itself.
- Implication for the diff viewer: must support **partial-file diffs** —
  pick a contiguous slice of a file's hunks rather than always rendering
  the file's full diff.

## Related code from outside the diff (with LSP)

Today `related_code` is sparsely populated by the ripgrep retriever and
mostly empty. To make it pull its weight:

- Treat **related_code** as strictly **out-of-diff** references — anything
  inside the diff belongs in the diff window itself, not in this section.
- Wire in an **LSP** to resolve real references: definitions, callsites,
  type info, references-of. Likely path: a small server that speaks LSP
  per-language (pyright / typescript-language-server / gopls / rust-analyzer)
  and exposes a "give me references for symbol X at file:line" tool to
  the narrator. The model decides what's worth surfacing.
- Alternative paths if LSP integration is too heavy: tree-sitter for
  symbol extraction + ripgrep for occurrences (cheaper, less precise);
  or use the Claude tool-use loop to let the model itself spelunk via
  filesystem reads.
- UI: snippet preview stays terse, but **click to expand** opens a
  lightweight modal showing the surrounding lines with syntax highlighting.
  Close on Esc / outside-click; keep the diff visible behind it. Avoid
  navigating away from the current chunk.

## Line-highlight alignment correctness

Observed: anchored segments occasionally highlight slightly the wrong
lines. Root cause likely the LLM emitting an approximate range
(probabilistic). Ideas to harden:

1. **Validate-then-snap.** After the LLM emits an anchor, verify the
   range overlaps a real diff hunk in that file. If it doesn't, snap
   to the nearest hunk's range.
2. **Symbol-anchored emission.** Have the LLM emit `anchor = { file,
   symbol: "stable_uid" }` or `anchor = { file, leading_line: "for block
   in busy_blocks:" }` instead of (or alongside) raw line numbers. A
   server-side resolver finds the line range in the actual diff. This
   trades precision for robustness — symbols are more stable than the
   model's arithmetic.
3. **Multi-pass extraction.** First pass: model emits segments with
   prose + a brief reference ("the constructor", "the UID hash line").
   Second pass: deterministic tool (or a smaller model) maps each
   reference to a line range using the diff text. The big model never
   does line arithmetic.
4. **Diff-aware enumeration in the prompt.** Today we list each hunk's
   start/end range. Could go finer — number each line in the prompt
   itself (`L42  cal.add("version", "2.0")`) so the model picks a literal
   number it has seen rather than computing one. Costs tokens; gains
   accuracy.
5. **Client-side fuzz.** When highlighting, if the exact range matches
   nothing, expand by ±2 lines and try again. Cheap fallback for small
   off-by-ones.

My lean for the first cut: (1) + (4) together. Validate-and-snap catches
the worst misses cheaply; line-numbered prompt reduces them at source.
(2) or (3) is the real fix if (1)+(4) isn't enough.
