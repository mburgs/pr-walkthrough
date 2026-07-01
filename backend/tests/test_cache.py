"""Persistent cache: put/get round-trip + LRU eviction."""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.schemas import (
    ChunkNarration,
    Hunk,
    NarrationSegment,
    PRMetadata,
    TourChunk,
    TourPlan,
)
from pr_walkthrough.cache import (
    PersistentCache,
    audio_cache_key,
    narration_cache_key,
    prompt_version,
    tour_plan_cache_key,
)


def _narration(chunk_id: str = "c1", text: str = "hello") -> ChunkNarration:
    return ChunkNarration(
        chunk_id=chunk_id,
        narration=text,
        intro=None,
        segments=[NarrationSegment(text=text, anchor=None)],
        segment_offsets_ms=[0],
        related_code=[],
        concerns=[],
    )


def test_narration_round_trip(tmp_path: Path) -> None:
    cache = PersistentCache(tmp_path / "c.db")
    key = narration_cache_key("foo/bar", "abc123", "c1", "review")
    assert cache.get_narration(key) is None

    n = _narration()
    cache.put_narration(key, n)
    got = cache.get_narration(key)
    assert got is not None
    assert got.chunk_id == "c1"
    assert got.narration == "hello"


def test_audio_round_trip(tmp_path: Path) -> None:
    cache = PersistentCache(tmp_path / "c.db")
    key = audio_cache_key("the narration text", "kokoro")
    assert cache.get_audio(key) is None

    cache.put_audio(key, b"RIFF...wav", [0, 250, 500])
    got = cache.get_audio(key)
    assert got is not None
    audio, offsets = got
    assert audio == b"RIFF...wav"
    assert offsets == [0, 250, 500]


def _tour_plan() -> TourPlan:
    pr = PRMetadata(
        url="https://x", repo="foo/bar", number=1, title="t", author="a",
        base_ref="main", head_ref="feat", base_sha="0" * 40, head_sha="1" * 40,
        body="",
    )
    hunk = Hunk(
        file="a.py", old_range=(0, 0), new_range=(1, 1),
        header="@@ -0,0 +1,1 @@", body="+x",
    )
    return TourPlan(
        session_id="sess_x",
        pr=pr,
        chunks=[
            TourChunk(
                chunk_id="c1", files=["a.py"], hunks=[hunk],
                summary="s", rationale_for_position="r", est_concern_level="low",
            ),
        ],
    )


def test_tour_plan_round_trip(tmp_path: Path) -> None:
    cache = PersistentCache(tmp_path / "c.db")
    key = tour_plan_cache_key("foo/bar", "abc123")
    assert cache.get_tour_plan(key) is None

    cache.put_tour_plan(key, _tour_plan())
    got = cache.get_tour_plan(key)
    assert got is not None
    assert got.pr.repo == "foo/bar"
    assert got.chunks[0].chunk_id == "c1"


def test_tour_plan_key_includes_prompt_version() -> None:
    key = tour_plan_cache_key("foo/bar", "abc123")
    assert prompt_version() in key


def test_prompt_version_is_stable() -> None:
    assert prompt_version() == prompt_version()
    assert len(prompt_version()) > 0


def test_keys_include_prompt_version() -> None:
    """Bumping the prompt should change the narration key — sanity that
    the cache key isn't blind to prompt edits."""
    key1 = narration_cache_key("r", "s", "c1", "review")
    assert prompt_version() in key1


def test_lru_evicts_when_over_cap(tmp_path: Path) -> None:
    # Cap chosen so two ~5KB audio rows fit but a third forces eviction.
    cache = PersistentCache(tmp_path / "c.db", max_bytes=12_000)
    payload = b"x" * 5_000
    cache.put_audio("a", payload, [])
    cache.put_audio("b", payload, [])
    assert cache.get_audio("a") is not None
    assert cache.get_audio("b") is not None

    # Touch 'a' so 'b' is the LRU victim
    cache.get_audio("a")
    cache.put_audio("c", payload, [])

    assert cache.get_audio("a") is not None, "freshly-touched row evicted"
    assert cache.get_audio("c") is not None, "just-inserted row evicted"
    # 'b' should have been evicted as the least-recently-used
    assert cache.get_audio("b") is None


def test_corrupt_narration_row_dropped(tmp_path: Path) -> None:
    cache = PersistentCache(tmp_path / "c.db")
    key = "garbage-key"
    # Write directly through the connection — bypass the validation.
    with cache._conn() as conn:
        conn.execute(
            "INSERT INTO narrations (key, narration_json, size_bytes, created_at, accessed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, "{not json}", 12, 0.0, 0.0),
        )
    assert cache.get_narration(key) is None
    # And the row should be gone after the dropped-on-read cleanup
    with cache._conn() as conn:
        row = conn.execute("SELECT 1 FROM narrations WHERE key = ?", (key,)).fetchone()
    assert row is None
