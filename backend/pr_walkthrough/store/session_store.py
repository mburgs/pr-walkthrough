"""SQLite-backed persistent store for sessions, chunk narrations, flags, and Q&A.

Uses only the stdlib sqlite3 module.  All JSON columns store model .model_dump_json().
Thread safety: every method opens a connection with check_same_thread=False and
WAL mode for concurrent reads under asyncio thread-pool calls.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from contracts.schemas import (
    ChunkNarration,
    Flag,
    FollowUp,
    FollowUpAnswer,
    SessionState,
    TourPlan,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    pr_url      TEXT NOT NULL,
    plan_json   TEXT NOT NULL,       -- TourPlan JSON
    current_chunk_id TEXT,
    created_at  REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS chunk_narrations (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    chunk_id    TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'review',  -- FamiliarityLevel
    narration_json TEXT NOT NULL,    -- ChunkNarration JSON
    audio_bytes BLOB,                -- cached WAV; NULL until synthesised
    PRIMARY KEY (session_id, chunk_id, level)
);

CREATE TABLE IF NOT EXISTS follow_ups (
    answer_id   TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    follow_up_json  TEXT NOT NULL,   -- FollowUp JSON
    answer_json TEXT NOT NULL,       -- FollowUpAnswer JSON
    audio_bytes BLOB,                -- spoken answer WAV
    created_at  REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS flags (
    flag_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    flag_json   TEXT NOT NULL        -- Flag JSON
);

-- One row per (session, chunk, tts engine, filtered) combo. The
-- audio-variants API populates this lazily on first request so the user
-- can A/B different engines/filters on the same narration.
CREATE TABLE IF NOT EXISTS audio_variants (
    session_id     TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    chunk_id       TEXT NOT NULL,
    engine         TEXT NOT NULL,
    filtered       INTEGER NOT NULL,
    audio_bytes    BLOB NOT NULL,
    offsets_json   TEXT NOT NULL,
    PRIMARY KEY (session_id, chunk_id, engine, filtered)
);

-- Cleanup indexes — without these, `WHERE session_id=? AND chunk_id=?`
-- queries against audio_variants / flags scan the table.
CREATE INDEX IF NOT EXISTS ix_audio_variants_session_chunk
    ON audio_variants(session_id, chunk_id);
CREATE INDEX IF NOT EXISTS ix_flags_session
    ON flags(session_id);
"""


class SessionStore:
    """Thread-safe SQLite store. Pass db_path=':memory:' for tests."""

    def __init__(self, db_path: str | Path = "sessions.db") -> None:
        # ":memory:" gives each connection its own private DB, which breaks
        # cross-thread access (TestClient uses a worker thread). Rewrite to a
        # shared-cache URI so every connection sees the same in-memory tables.
        raw = str(db_path)
        if raw == ":memory:":
            self._db_path = "file::memory:?cache=shared"
            self._uri = True
        else:
            self._db_path = raw
            self._uri = False
        self._local = threading.local()
        # Touch one connection so schema exists before anyone else opens one.
        with self._conn() as _:
            pass

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,  # autocommit; we use explicit transactions
                uri=self._uri,
            )
            if not self._uri:
                conn.execute("PRAGMA journal_mode=WAL")
            # SQLite defaults `foreign_keys=OFF`, so all the REFERENCES
            # clauses above are otherwise no-ops. Turn them on per-connection
            # — once cascade is real, deleting a session collects its narration
            # / flag / follow-up / audio_variants rows automatically.
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            # Dev-only migration: if an old `chunk_narrations` table exists
            # without the `level` column, drop it before the CREATE TABLE
            # IF NOT EXISTS runs. There's no production data + the schema
            # carries no semantic meaning across sessions, so wiping is
            # safer than half-migrating.
            existing = conn.execute("PRAGMA table_info(chunk_narrations)").fetchall()
            if existing and not any(c["name"] == "level" for c in existing):
                conn.execute("DROP TABLE chunk_narrations")
            # CREATE TABLE IF NOT EXISTS is idempotent — safe to run per-connection.
            # Required so worker-thread connections also see the schema (esp. for
            # shared-cache in-memory, where the DB persists but each connection
            # still needs its own setup pass at minimum once).
            conn.executescript(_SCHEMA)
            self._local.conn = conn
        yield self._local.conn

    # ------------------------------------------------------------------ sessions

    def create_session(self, plan: TourPlan) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, pr_url, plan_json) VALUES (?, ?, ?)",
                (plan.session_id, plan.pr.url, plan.model_dump_json()),
            )

    def get_session_state(self, session_id: str) -> SessionState | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT plan_json, current_chunk_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        plan = TourPlan.model_validate_json(row["plan_json"])
        flags = self.list_flags(session_id)
        return SessionState(
            plan=plan,
            current_chunk_id=row["current_chunk_id"],
            flags=flags,
        )

    def update_current_chunk(self, session_id: str, chunk_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET current_chunk_id = ? WHERE session_id = ?",
                (chunk_id, session_id),
            )

    # ------------------------------------------------------------------ narrations

    def save_narration(
        self, session_id: str, narration: ChunkNarration, level: str = "review",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chunk_narrations (session_id, chunk_id, level, narration_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id, chunk_id, level) DO UPDATE SET narration_json=excluded.narration_json""",
                (session_id, narration.chunk_id, level, narration.model_dump_json()),
            )

    def get_narration(
        self, session_id: str, chunk_id: str, level: str = "review",
    ) -> ChunkNarration | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT narration_json FROM chunk_narrations WHERE session_id=? AND chunk_id=? AND level=?",
                (session_id, chunk_id, level),
            ).fetchone()
        if row is None:
            return None
        return ChunkNarration.model_validate_json(row["narration_json"])

    def save_chunk_audio(
        self, session_id: str, chunk_id: str, audio: bytes, level: str = "review",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chunk_narrations (session_id, chunk_id, level, narration_json, audio_bytes)
                   VALUES (?, ?, ?, '', ?)
                   ON CONFLICT(session_id, chunk_id, level) DO UPDATE SET audio_bytes=excluded.audio_bytes""",
                (session_id, chunk_id, level, audio),
            )

    def delete_chunk_cache(self, session_id: str, chunk_id: str) -> None:
        """Wipe one chunk's narration + audio + all variants across all levels.

        Used by the regenerate endpoint to force the worker to re-narrate
        and re-synth — useful when the user has updated the narrate prompt
        and wants to see the new output without restarting the session.
        """
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM chunk_narrations WHERE session_id=? AND chunk_id=?",
                (session_id, chunk_id),
            )
            conn.execute(
                "DELETE FROM audio_variants WHERE session_id=? AND chunk_id=?",
                (session_id, chunk_id),
            )

    def get_chunk_audio(
        self, session_id: str, chunk_id: str, level: str = "review",
    ) -> bytes | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT audio_bytes FROM chunk_narrations WHERE session_id=? AND chunk_id=? AND level=?",
                (session_id, chunk_id, level),
            ).fetchone()
        if row is None or row["audio_bytes"] is None:
            return None
        return bytes(row["audio_bytes"])

    # ------------------------------------------------------------------ audio variants

    def save_audio_variant(
        self,
        session_id: str,
        chunk_id: str,
        engine: str,
        filtered: bool,
        audio: bytes,
        offsets_ms: list[int],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO audio_variants
                   (session_id, chunk_id, engine, filtered, audio_bytes, offsets_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, chunk_id, engine, filtered) DO UPDATE SET
                       audio_bytes=excluded.audio_bytes,
                       offsets_json=excluded.offsets_json""",
                (session_id, chunk_id, engine, 1 if filtered else 0,
                 audio, json.dumps(offsets_ms)),
            )

    def get_audio_variant(
        self,
        session_id: str,
        chunk_id: str,
        engine: str,
        filtered: bool,
    ) -> tuple[bytes, list[int]] | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT audio_bytes, offsets_json FROM audio_variants
                   WHERE session_id=? AND chunk_id=? AND engine=? AND filtered=?""",
                (session_id, chunk_id, engine, 1 if filtered else 0),
            ).fetchone()
        if row is None:
            return None
        return bytes(row["audio_bytes"]), json.loads(row["offsets_json"])

    def list_audio_variants(
        self, session_id: str, chunk_id: str
    ) -> list[tuple[str, bool]]:
        """Return list of (engine, filtered) tuples already synth'd for this chunk."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT engine, filtered FROM audio_variants
                   WHERE session_id=? AND chunk_id=?""",
                (session_id, chunk_id),
            ).fetchall()
        return [(r["engine"], bool(r["filtered"])) for r in rows]

    # ------------------------------------------------------------------ follow-ups

    def save_follow_up(
        self,
        session_id: str,
        follow_up: FollowUp,
        answer: FollowUpAnswer,
    ) -> str:
        answer_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO follow_ups (answer_id, session_id, follow_up_json, answer_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    answer_id,
                    session_id,
                    follow_up.model_dump_json(),
                    answer.model_dump_json(),
                ),
            )
        return answer_id

    def save_follow_up_audio(self, session_id: str, answer_id: str, audio: bytes) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE follow_ups SET audio_bytes=? WHERE answer_id=? AND session_id=?",
                (audio, answer_id, session_id),
            )

    def get_follow_up_audio(self, session_id: str, answer_id: str) -> bytes | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT audio_bytes FROM follow_ups WHERE answer_id=? AND session_id=?",
                (answer_id, session_id),
            ).fetchone()
        if row is None or row["audio_bytes"] is None:
            return None
        return bytes(row["audio_bytes"])

    def list_narrated_chunks(self, session_id: str) -> list[ChunkNarration]:
        """Return all narrations seen so far, for LLM context.

        Renamed from `list_follow_up_history` — that name was misleading;
        these are ChunkNarration rows from the narration table, not prior
        follow-up Q&A. For the latter, use `list_follow_up_qa`.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT narration_json FROM chunk_narrations WHERE session_id=? AND narration_json != ''",
                (session_id,),
            ).fetchall()
        return [ChunkNarration.model_validate_json(r["narration_json"]) for r in rows]

    def list_follow_up_qa(
        self, session_id: str
    ) -> list[tuple[FollowUp, FollowUpAnswer]]:
        """All prior follow-up Q&A pairs for this session, oldest-first.

        Used to replay conversation history as messages[] to the LLM so
        each new follow-up sees what was already asked/answered. Ordered
        by sqlite rowid ascending (insertion order); created_at would
        also work but rowid avoids tie-breaking when two inserts share
        the same unixepoch second.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT follow_up_json, answer_json
                   FROM follow_ups WHERE session_id=?
                   ORDER BY rowid ASC""",
                (session_id,),
            ).fetchall()
        return [
            (
                FollowUp.model_validate_json(r["follow_up_json"]),
                FollowUpAnswer.model_validate_json(r["answer_json"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ flags

    def create_flag(self, session_id: str, flag: Flag) -> Flag:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO flags (flag_id, session_id, flag_json) VALUES (?, ?, ?)",
                (flag.flag_id, session_id, flag.model_dump_json()),
            )
        return flag

    def get_flag(self, session_id: str, flag_id: str) -> Flag | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT flag_json FROM flags WHERE flag_id=? AND session_id=?",
                (flag_id, session_id),
            ).fetchone()
        if row is None:
            return None
        return Flag.model_validate_json(row["flag_json"])

    def update_flag(self, session_id: str, flag: Flag) -> Flag:
        with self._conn() as conn:
            conn.execute(
                "UPDATE flags SET flag_json=? WHERE flag_id=? AND session_id=?",
                (flag.model_dump_json(), flag.flag_id, session_id),
            )
        return flag

    def delete_flag(self, session_id: str, flag_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM flags WHERE flag_id=? AND session_id=?",
                (flag_id, session_id),
            )
        return cur.rowcount > 0

    def list_flags(self, session_id: str) -> list[Flag]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT flag_json FROM flags WHERE session_id=?",
                (session_id,),
            ).fetchall()
        return [Flag.model_validate_json(r["flag_json"]) for r in rows]
