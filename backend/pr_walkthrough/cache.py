"""Persistent content-addressed cache for narration + TTS audio.

The SessionStore (sqlite, `backend/sessions.db`) is keyed by random
`session_id` — it doesn't survive restart in any meaningful way, since a
fresh CLI run produces a fresh session_id and re-issues every LLM + TTS
call. This cache sits parallel to it and is keyed by content:

  narration : (repo_slug, head_sha, chunk_id, level, prompt_version)
  audio     : (narration_hash, engine)

`prompt_version` is the SHA-1 of the prompts module source so editing the
prompt template auto-invalidates downstream rows without a manual bump.

Lives at `~/.cache/pr-walkthrough/cache.db` (or `$XDG_CACHE_HOME` if set).
LRU-evicted to a configurable byte cap on every write — small writes
hardly ever trigger eviction; the only chunky rows are audio blobs.

Opt-in: callers construct one only when `Config.cache.enabled` is true.
The chunk worker treats `ctx.cache is None` as "skip cache, do the work".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from contracts.schemas import ChunkNarration

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS narrations (
    key            TEXT PRIMARY KEY,
    narration_json TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    created_at     REAL NOT NULL,
    accessed_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audio (
    key          TEXT PRIMARY KEY,
    audio_bytes  BLOB NOT NULL,
    offsets_json TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    created_at   REAL NOT NULL,
    accessed_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_narrations_accessed ON narrations(accessed_at);
CREATE INDEX IF NOT EXISTS ix_audio_accessed      ON audio(accessed_at);
"""


def default_cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "pr-walkthrough"


def default_cache_path() -> Path:
    return default_cache_dir() / "cache.db"


def _hash_str(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


_PROMPT_VERSION_CACHE: str | None = None


def prompt_version() -> str:
    """SHA-1 of the prompts module source (truncated). Memoised — the
    file doesn't change at runtime."""
    global _PROMPT_VERSION_CACHE
    if _PROMPT_VERSION_CACHE is None:
        try:
            from pr_walkthrough.llm import prompts as _prompts
            src = Path(_prompts.__file__).read_text(encoding="utf-8")
            _PROMPT_VERSION_CACHE = _hash_str(src)
        except Exception:
            _PROMPT_VERSION_CACHE = "unknown"
    return _PROMPT_VERSION_CACHE


def narration_cache_key(
    repo_slug: str,
    head_sha: str,
    chunk_id: str,
    level: str,
) -> str:
    return f"{repo_slug}|{head_sha}|{chunk_id}|{level}|{prompt_version()}"


def audio_cache_key(narration_text: str, engine: str) -> str:
    return f"{_hash_str(narration_text)}|{engine}"


class PersistentCache:
    """SQLite-backed content cache. Opt-in.

    Reads return None on miss; writes evict to the byte cap if the
    insert would push us over.
    """

    def __init__(self, db_path: Path | None = None, max_bytes: int = 1_073_741_824) -> None:
        self._db_path = str(db_path or default_cache_path())
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max_bytes
        self._local = threading.local()
        with self._conn() as _:
            pass  # warm up schema

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            self._local.conn = conn
        yield self._local.conn

    # ----------------------------------------------------------- narration

    def get_narration(self, key: str) -> ChunkNarration | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT narration_json FROM narrations WHERE key = ?", (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            n = ChunkNarration.model_validate_json(row["narration_json"])
        except Exception:
            log.warning("corrupt narration cache row for %s; dropping", key)
            self._delete_narration(key)
            return None
        self._touch("narrations", key)
        return n

    def put_narration(self, key: str, narration: ChunkNarration) -> None:
        payload = narration.model_dump_json()
        size = len(payload.encode("utf-8"))
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO narrations (key, narration_json, size_bytes, created_at, accessed_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       narration_json=excluded.narration_json,
                       size_bytes=excluded.size_bytes,
                       accessed_at=excluded.accessed_at""",
                (key, payload, size, now, now),
            )
        self._maybe_evict()

    def _delete_narration(self, key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM narrations WHERE key = ?", (key,))

    # --------------------------------------------------------------- audio

    def get_audio(self, key: str) -> tuple[bytes, list[int]] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT audio_bytes, offsets_json FROM audio WHERE key = ?", (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            offsets = json.loads(row["offsets_json"])
        except json.JSONDecodeError:
            offsets = []
        self._touch("audio", key)
        return bytes(row["audio_bytes"]), offsets

    def put_audio(self, key: str, audio: bytes, offsets_ms: list[int]) -> None:
        size = len(audio) + len(json.dumps(offsets_ms))
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO audio (key, audio_bytes, offsets_json, size_bytes, created_at, accessed_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       audio_bytes=excluded.audio_bytes,
                       offsets_json=excluded.offsets_json,
                       size_bytes=excluded.size_bytes,
                       accessed_at=excluded.accessed_at""",
                (key, audio, json.dumps(offsets_ms), size, now, now),
            )
        self._maybe_evict()

    # --------------------------------------------------------- maintenance

    def _touch(self, table: str, key: str) -> None:
        with self._conn() as conn:
            conn.execute(
                f"UPDATE {table} SET accessed_at = ? WHERE key = ?",
                (time.time(), key),
            )

    def total_bytes(self) -> int:
        with self._conn() as conn:
            n = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM narrations").fetchone()[0]
            a = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM audio").fetchone()[0]
        return int(n) + int(a)

    def _maybe_evict(self) -> None:
        """LRU eviction. Cheap when cache is well under the cap; only
        starts deleting when we're over. Audio rows are O(MB), narrations
        are O(KB) — audio dominates the cap, so we prefer evicting audio
        first when both are candidates of similar age."""
        used = self.total_bytes()
        if used <= self._max_bytes:
            return
        target = int(self._max_bytes * 0.9)  # leave 10% headroom after eviction
        with self._conn() as conn:
            # Union the two tables by accessed_at ascending — oldest first.
            rows = conn.execute(
                """
                SELECT 'audio' AS tbl, key, size_bytes, accessed_at FROM audio
                UNION ALL
                SELECT 'narrations' AS tbl, key, size_bytes, accessed_at FROM narrations
                ORDER BY accessed_at ASC
                """
            ).fetchall()
            for r in rows:
                if used <= target:
                    break
                conn.execute(f"DELETE FROM {r['tbl']} WHERE key = ?", (r["key"],))
                used -= int(r["size_bytes"])
        log.info("pr-walkthrough cache evicted to %.1f MB", used / 1e6)
