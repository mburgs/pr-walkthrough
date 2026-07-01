"""Deprecated. Replaced by the LSP retriever (pyright/pylsp) + ripgrep
fallback. This shim exists only to avoid breaking any out-of-tree
imports during the rollout; the real code lives in
`pr_walkthrough.context.lsp.retriever` and
`pr_walkthrough.context.retriever`.

A future cleanup pass can delete this module entirely.
"""

from __future__ import annotations

import logging

from pr_walkthrough.context.lsp.pool import LSPPool
from pr_walkthrough.context.lsp.retriever import LSPContextRetriever
from pr_walkthrough.context.retriever import RipgrepContextRetriever

log = logging.getLogger(__name__)


class HybridContextRetriever:
    """LSP first (per language) → ripgrep fallback. Language-agnostic
    fallback by design — Jedi (Python-only) was removed once pyright
    became the recommended path."""

    def __init__(self, configured_lsp_paths: dict[str, str] | None = None) -> None:
        self._pool = LSPPool(configured_lsp_paths)
        self._lsp = LSPContextRetriever(self._pool)
        self._rg = RipgrepContextRetriever()

    async def related(self, anchor, repo_root, seed_lines=None):
        # Try LSP first if a server for this language is available; on
        # failure or empty result, fall back to ripgrep. Empty is
        # treated as "no idea" not "definitively no related code" —
        # the ripgrep pass might still surface useful sibling code.
        if self._lsp.is_available(anchor.file):
            try:
                hits = await self._lsp.related(anchor, repo_root, seed_lines=seed_lines)
                if hits:
                    return hits
            except Exception:
                log.warning("LSP retriever raised; falling back to ripgrep", exc_info=True)
        return await self._rg.related(anchor, repo_root, seed_lines=seed_lines)

    async def aclose(self) -> None:
        await self._pool.aclose()
