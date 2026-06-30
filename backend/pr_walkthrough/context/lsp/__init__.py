"""LSP-backed cross-repo context retrieval.

The Jedi + ripgrep retrievers handle Python and the fallback case; this
package adds real find-references / go-to-definition via language servers
so TS/JS gets the same precision Python had via Jedi, and other languages
gain it for the first time.

Layout:

  client.py        — async JSON-RPC client over stdio
  server.py        — language-server lifecycle (spawn, initialize, kill)
  pool.py          — keep one server alive per (language, repo_root)
  retriever.py     — ContextRetriever protocol implementation
  detect.py        — language detection from file paths + binary discovery
"""
