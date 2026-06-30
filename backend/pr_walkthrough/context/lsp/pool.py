"""Server pool — one running LSP per (language, repo_root).

Spawning a language server is expensive (pyright indexes the workspace
on startup), so we keep them alive for the lifetime of the AppContext
and shut them all down on `aclose`.

Concurrent first-use is guarded by a per-key asyncio.Lock so two chunks
that arrive at the same time for the same language don't race-spawn two
copies of pyright.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .client import LSPClient, LSPError
from .detect import resolve_server_command

log = logging.getLogger(__name__)


class LSPPool:
    """Lazy pool of LSPClients keyed by (language, repo_root)."""

    def __init__(self, configured_paths: dict[str, str] | None = None) -> None:
        self._configured = dict(configured_paths or {})
        self._clients: dict[tuple[str, Path], LSPClient] = {}
        self._locks: dict[tuple[str, Path], asyncio.Lock] = {}
        # Languages we've already failed to launch — short-circuit so the
        # caller doesn't spend 30s every chunk waiting for an LSPError.
        self._unavailable: set[tuple[str, Path]] = set()

    def is_available(self, language: str) -> bool:
        """Cheap pre-check (PATH lookup only) — doesn't spawn anything."""
        return resolve_server_command(language, self._configured.get(language)) is not None

    async def get(self, language: str, repo_root: Path) -> LSPClient | None:
        """Return a ready-to-use client, or None if unavailable.

        On first call for a (lang, repo) pair, spawns the server, sends
        initialize, and caches it. Subsequent calls reuse the same
        client.
        """
        key = (language, repo_root.resolve())
        if key in self._unavailable:
            return None
        existing = self._clients.get(key)
        if existing is not None:
            return existing

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Double-check now that we hold the lock
            existing = self._clients.get(key)
            if existing is not None:
                return existing
            if key in self._unavailable:
                return None

            cmd = resolve_server_command(language, self._configured.get(language))
            if cmd is None:
                self._unavailable.add(key)
                return None

            log.info("spawning LSP server %s for %s in %s", cmd[0], language, repo_root)
            try:
                client = await LSPClient.spawn(cmd, cwd=repo_root)
                await client.initialize(_path_to_uri(repo_root))
            except (LSPError, OSError, FileNotFoundError) as exc:
                log.warning("LSP %s failed to start: %s", cmd[0], exc)
                self._unavailable.add(key)
                return None
            self._clients[key] = client
            return client

    async def aclose(self) -> None:
        """Shut down every running server."""
        clients = list(self._clients.values())
        self._clients.clear()
        for c in clients:
            try:
                await c.shutdown()
            except Exception:  # pragma: no cover - cleanup
                pass


def _path_to_uri(p: Path) -> str:
    return p.resolve().as_uri()
