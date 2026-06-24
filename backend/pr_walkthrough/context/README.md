# context — ripgrep-based related-code retrieval

**Stream 7** of the pr-walkthrough parallel build.

## System dependency

`rg` (ripgrep) must be on `PATH`.  Install via:

| OS | Command |
|----|---------|
| macOS | `brew install ripgrep` |
| Ubuntu/Debian | `apt install ripgrep` |
| Arch | `pacman -S ripgrep` |
| Windows | `winget install BurntSushi.ripgrep.MSVC` |
| Cargo | `cargo install ripgrep` |

The retriever will raise `RipgrepNotFoundError` with an installation hint if
`rg` is missing.

## Design rationale

**Why ripgrep?**  It is fast, handles large repos with ease, outputs
structured JSON (`--json`), and is language-agnostic — no per-language
parsers are required.  A single `rg -w <symbol>` run across a 10 k-file
repo typically completes in < 200 ms on modern hardware.

**Heuristics over parsing.**  Classification (definition / callsite / test /
sibling) is intentionally done with regular expressions rather than a real
AST.  This keeps the implementation dependency-free and works across any
language.  False positives are rare enough for the display use-case — the
reviewer still sees the snippet and can judge for themselves.

**No AI ranking.**  Results are ranked purely by relationship priority
(definition > test > callsite > sibling) and capped at 8.  This is
deliberately boring: deterministic, fast, testable.

**Memory-efficient snippet extraction.**  The retriever builds a
byte-offset index of each file and reads only the snippet window (match ±5
lines) via `seek()`, so large files are never held in memory.

**`prior_version` not implemented in v1.**  Retrieving prior versions would
require `git log -S <symbol>` or `git log -p`, which couples the retriever
to a git repository.  The interface already exposes the relationship kind;
the implementation can be added in a future PR.

## What LSP would add

A Language Server Protocol (LSP) integration would replace the heuristic
classification step with precise workspace-level semantics.  Specifically,
`textDocument/definition` gives an exact go-to-definition result for the
symbol under the cursor (resolving imports and overloads), while
`textDocument/references` returns every call site with full type context,
eliminating false positives from name collisions.  The upgrade path is to
launch a language-specific server (e.g. `pylsp` for Python, `gopls` for Go,
`typescript-language-server` for TypeScript) as a subprocess, send
JSON-RPC requests for the anchor position, and use the LSP responses to
re-rank or replace the ripgrep results.  The ripgrep pass would remain as a
fast pre-filter for languages that lack an LSP or when the server takes too
long to index.

## CLI usage

```
python -m pr_walkthrough.context.cli <repo-root> <file>:<start>-<end>
```

Example (run from the worktree root):

```
python -m pr_walkthrough.context.cli \
    backend/pr_walkthrough/context/tests/fixtures/sample_repo \
    sample_repo/store.py:24-26
```

Prints a JSON array of `RelatedCode` objects to stdout.

## Running tests

```
cd backend
pytest pr_walkthrough/context/tests/ -v
```
