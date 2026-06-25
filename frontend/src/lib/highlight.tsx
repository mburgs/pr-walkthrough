/**
 * Tiny Prism-via-refractor wrapper used by both DiffViewer (line tokens) and
 * RightRail's Related snippets. Resolves a hast tree into nested spans whose
 * `class` matches our Prism token rules in index.css.
 *
 * refractor v5 returns a `Root` whose children are the highlighted nodes;
 * react-diff-view's tokenize wraps in its own root, so its adapter wants
 * `.children` directly. For raw <pre> rendering we want the whole tree.
 */
import { refractor } from "refractor/all";

export type HastNode =
  | { type: "text"; value: string }
  | { type: "element"; tagName: string; properties?: { className?: string[] }; children: HastNode[] };

const EXT_TO_LANG: Record<string, string> = {
  ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx",
  mjs: "javascript", cjs: "javascript",
  py: "python", pyi: "python",
  go: "go",
  rs: "rust",
  java: "java", kt: "kotlin", kts: "kotlin",
  rb: "ruby",
  php: "php",
  c: "c", h: "c", cpp: "cpp", cxx: "cpp", cc: "cpp", hpp: "cpp",
  cs: "csharp",
  swift: "swift",
  sh: "bash", bash: "bash", zsh: "bash",
  sql: "sql",
  json: "json", yaml: "yaml", yml: "yaml", toml: "toml",
  md: "markdown",
  html: "markup", xml: "markup", svg: "markup",
  css: "css", scss: "scss", sass: "sass", less: "less",
};

export function languageFor(path: string): string | null {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return EXT_TO_LANG[ext] ?? null;
}

/** Highlight a raw code string into hast nodes, or null on failure. */
export function highlightSnippet(code: string, language: string | null): HastNode[] | null {
  if (!language) return null;
  try {
    const root = refractor.highlight(code, language) as { children?: HastNode[] };
    return root.children ?? null;
  } catch {
    return null;
  }
}

/** Convert a hast tree (with className arrays) into React-friendly JSX. */
export function renderHast(nodes: HastNode[]): React.ReactNode {
  return nodes.map((n, i) => {
    if (n.type === "text") return n.value;
    const cls = n.properties?.className?.join(" ");
    return (
      // Refractor only emits <span> elements for tokens
      // eslint-disable-next-line react/no-array-index-key
      <span key={i} className={cls}>
        {renderHast(n.children)}
      </span>
    );
  }) as unknown as React.ReactNode;
}
