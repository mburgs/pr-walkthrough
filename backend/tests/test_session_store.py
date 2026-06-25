"""Tests for SessionStore: schema lifecycle, audio variants, cache deletion."""

from __future__ import annotations

import json
import uuid
import pytest

from contracts.schemas import (
    ChunkNarration, CodeAnchor, NarrationSegment, PRMetadata, TourChunk, TourPlan,
)
from pr_walkthrough.store import SessionStore


def _make_plan(session_id: str | None = None) -> TourPlan:
    return TourPlan(
        session_id=session_id or f"sess_{uuid.uuid4().hex[:12]}",
        pr=PRMetadata(
            url="https://github.com/x/y/pull/1",
            repo="x/y",
            number=1,
            title="t",
            author="a",
            base_ref="main",
            head_ref="f",
            base_sha="a" * 40,
            head_sha="b" * 40,
        ),
        chunks=[TourChunk(
            chunk_id="c1",
            files=["a.py"],
            hunks=[],
            summary="x",
            rationale_for_position="x",
            est_concern_level="low",
        )],
    )


def _make_narration() -> ChunkNarration:
    return ChunkNarration(
        chunk_id="c1",
        narration="one. two.",
        segments=[
            NarrationSegment(text="one.", anchor=CodeAnchor(file="a.py", line_range=(1, 1))),
            NarrationSegment(text="two.", anchor=None),
        ],
        segment_offsets_ms=[0, 500],
        related_code=[],
        concerns=[],
        look_closer_for=[],
    )


# ---------------------------------------------------------------------------
# Cross-thread / in-memory durability
# ---------------------------------------------------------------------------


class TestInMemoryStore:
    def test_schema_visible_across_connections(self) -> None:
        """SessionStore(':memory:') must use a shared-cache URI so worker-thread
        connections see the schema that the main thread created."""
        import threading

        s = SessionStore(":memory:")
        errs: list[Exception] = []

        def worker() -> None:
            try:
                with s._conn() as c:
                    rows = c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                    names = {r["name"] for r in rows}
                    assert {"sessions", "chunk_narrations",
                            "audio_variants", "follow_ups", "flags"} <= names
            except Exception as e:
                errs.append(e)

        t = threading.Thread(target=worker)
        t.start(); t.join()
        assert errs == []


# ---------------------------------------------------------------------------
# Narration + chunk audio
# ---------------------------------------------------------------------------


class TestNarrationAndAudio:
    def test_save_and_get_roundtrip(self) -> None:
        s = SessionStore(":memory:")
        plan = _make_plan()
        s.create_session(plan)
        n = _make_narration()
        s.save_narration(plan.session_id, n)

        got = s.get_narration(plan.session_id, "c1")
        assert got is not None
        assert got.narration == "one. two."
        assert len(got.segments) == 2
        assert got.segment_offsets_ms == [0, 500]
        assert got.segments[0].anchor == CodeAnchor(file="a.py", line_range=(1, 1))

    def test_save_chunk_audio_then_get(self) -> None:
        s = SessionStore(":memory:")
        plan = _make_plan()
        s.create_session(plan)
        s.save_chunk_audio(plan.session_id, "c1", b"RIFF\x00\x00\x00\x00WAVE")
        assert s.get_chunk_audio(plan.session_id, "c1") == b"RIFF\x00\x00\x00\x00WAVE"


# ---------------------------------------------------------------------------
# Audio variants
# ---------------------------------------------------------------------------


class TestAudioVariants:
    def test_save_and_get_variant(self) -> None:
        s = SessionStore(":memory:")
        plan = _make_plan(); s.create_session(plan)
        s.save_audio_variant(plan.session_id, "c1", "kokoro", True, b"WAV1", [0, 100])
        got = s.get_audio_variant(plan.session_id, "c1", "kokoro", True)
        assert got is not None
        audio, offsets = got
        assert audio == b"WAV1"
        assert offsets == [0, 100]

    def test_filtered_true_and_false_are_separate_rows(self) -> None:
        s = SessionStore(":memory:")
        plan = _make_plan(); s.create_session(plan)
        s.save_audio_variant(plan.session_id, "c1", "kokoro", True, b"FILT", [0])
        s.save_audio_variant(plan.session_id, "c1", "kokoro", False, b"RAW", [10])

        f = s.get_audio_variant(plan.session_id, "c1", "kokoro", True)
        r = s.get_audio_variant(plan.session_id, "c1", "kokoro", False)
        assert f is not None and r is not None
        assert f[0] == b"FILT" and r[0] == b"RAW"
        assert f[1] == [0] and r[1] == [10]

    def test_upsert_overwrites_existing_variant(self) -> None:
        s = SessionStore(":memory:")
        plan = _make_plan(); s.create_session(plan)
        s.save_audio_variant(plan.session_id, "c1", "kokoro", True, b"old", [0])
        s.save_audio_variant(plan.session_id, "c1", "kokoro", True, b"new", [50])
        got = s.get_audio_variant(plan.session_id, "c1", "kokoro", True)
        assert got is not None
        assert got == (b"new", [50])

    def test_list_audio_variants_returns_all_engine_filter_combos(self) -> None:
        s = SessionStore(":memory:")
        plan = _make_plan(); s.create_session(plan)
        s.save_audio_variant(plan.session_id, "c1", "kokoro", True, b"a", [0])
        s.save_audio_variant(plan.session_id, "c1", "xtts", False, b"b", [0])
        s.save_audio_variant(plan.session_id, "c1", "f5", True, b"c", [0])
        out = set(s.list_audio_variants(plan.session_id, "c1"))
        assert out == {("kokoro", True), ("xtts", False), ("f5", True)}

    def test_get_missing_variant_returns_none(self) -> None:
        s = SessionStore(":memory:")
        plan = _make_plan(); s.create_session(plan)
        assert s.get_audio_variant(plan.session_id, "c1", "kokoro", True) is None


# ---------------------------------------------------------------------------
# delete_chunk_cache (used by /regenerate)
# ---------------------------------------------------------------------------


class TestDeleteChunkCache:
    def test_wipes_narration_audio_and_all_variants(self) -> None:
        s = SessionStore(":memory:")
        plan = _make_plan(); s.create_session(plan)
        n = _make_narration()
        s.save_narration(plan.session_id, n)
        s.save_chunk_audio(plan.session_id, "c1", b"WAV")
        s.save_audio_variant(plan.session_id, "c1", "kokoro", True, b"K1", [0])
        s.save_audio_variant(plan.session_id, "c1", "xtts", False, b"X1", [0])

        s.delete_chunk_cache(plan.session_id, "c1")

        assert s.get_narration(plan.session_id, "c1") is None
        assert s.get_chunk_audio(plan.session_id, "c1") is None
        assert s.list_audio_variants(plan.session_id, "c1") == []

    def test_leaves_other_chunks_alone(self) -> None:
        s = SessionStore(":memory:")
        plan = _make_plan(); s.create_session(plan)
        n1 = _make_narration()
        n2 = _make_narration().model_copy(update={"chunk_id": "c2"})
        s.save_narration(plan.session_id, n1)
        s.save_narration(plan.session_id, n2)
        s.save_audio_variant(plan.session_id, "c1", "kokoro", True, b"K1", [0])
        s.save_audio_variant(plan.session_id, "c2", "kokoro", True, b"K2", [0])

        s.delete_chunk_cache(plan.session_id, "c1")

        assert s.get_narration(plan.session_id, "c2") is not None
        assert s.get_audio_variant(plan.session_id, "c2", "kokoro", True) is not None
