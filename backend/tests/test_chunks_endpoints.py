"""Tests for the chunks API surface:

  - GET /chunks/{cid}/audio                — long-poll for cached WAV
  - GET /chunks/{cid}/audio/variants       — list engines + cached combos
  - GET /chunks/{cid}/audio.variant?...    — lazy-synth per variant
  - POST /chunks/{cid}/regenerate          — wipe + re-kick the worker

The fixture `client` uses FakePR + FakeLLM + FakeTTS — every variant
synthesises near-instantly to a short silent WAV.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient


PR_URL = "https://github.com/example-org/auth-service/pull/142"


def _create_session(client: TestClient) -> str:
    resp = client.post("/sessions", json={"pr_url": PR_URL})
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _wait_for_chunk_audio(client: TestClient, sid: str, cid: str) -> bytes:
    """The audio endpoint long-polls until the worker has saved a WAV."""
    resp = client.get(f"/sessions/{sid}/chunks/{cid}/audio")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content[:4] == b"RIFF"
    return resp.content


# ---------------------------------------------------------------------------
# /audio endpoint — long-poll behaviour
# ---------------------------------------------------------------------------


class TestAudioEndpoint:
    def test_returns_riff_wav_for_first_chunk(self, client: TestClient) -> None:
        sid = _create_session(client)
        audio = _wait_for_chunk_audio(client, sid, "c1")
        assert audio[8:12] == b"WAVE"

    def test_404_on_unknown_session(self, client: TestClient) -> None:
        resp = client.get("/sessions/sess_nope/chunks/c1/audio")
        assert resp.status_code == 404

    def test_serves_cached_audio_on_repeat_calls(self, client: TestClient) -> None:
        sid = _create_session(client)
        first = _wait_for_chunk_audio(client, sid, "c1")
        second = client.get(f"/sessions/{sid}/chunks/c1/audio").content
        assert first == second


# ---------------------------------------------------------------------------
# /audio/variants
# ---------------------------------------------------------------------------


class TestAudioVariantsList:
    def test_lists_registered_engines(self, client: TestClient) -> None:
        sid = _create_session(client)
        _wait_for_chunk_audio(client, sid, "c1")  # make sure narration exists
        resp = client.get(f"/sessions/{sid}/chunks/c1/audio/variants")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["engines"]) == {"kokoro", "xtts", "f5"}

    def test_cached_grows_after_a_variant_is_fetched(self, client: TestClient) -> None:
        sid = _create_session(client)
        _wait_for_chunk_audio(client, sid, "c1")

        before = client.get(f"/sessions/{sid}/chunks/c1/audio/variants").json()
        assert before["cached"] == [] or all(
            c["engine"] != "xtts" for c in before["cached"]
        )

        client.get(
            f"/sessions/{sid}/chunks/c1/audio.variant",
            params={"engine": "xtts", "filtered": True},
        )

        after = client.get(f"/sessions/{sid}/chunks/c1/audio/variants").json()
        assert {"engine": "xtts", "filtered": True} in after["cached"]


# ---------------------------------------------------------------------------
# /audio.variant — lazy synth + offsets header
# ---------------------------------------------------------------------------


class TestAudioVariant:
    def test_returns_wav_plus_offsets_header(self, client: TestClient) -> None:
        sid = _create_session(client)
        _wait_for_chunk_audio(client, sid, "c1")

        resp = client.get(
            f"/sessions/{sid}/chunks/c1/audio.variant",
            params={"engine": "kokoro", "filtered": True},
        )
        assert resp.status_code == 200
        assert resp.content[:4] == b"RIFF"

        offsets_json = resp.headers["X-Segment-Offsets-Ms"]
        offsets = json.loads(offsets_json)
        assert isinstance(offsets, list)
        # The fixture narration in FakeLLM has 3 segments
        assert len(offsets) >= 1
        # Offsets must be monotonically non-decreasing
        assert offsets == sorted(offsets)
        # First offset starts at 0
        assert offsets[0] == 0

    def test_cors_exposes_offsets_header(self, client: TestClient) -> None:
        sid = _create_session(client)
        _wait_for_chunk_audio(client, sid, "c1")
        resp = client.get(
            f"/sessions/{sid}/chunks/c1/audio.variant",
            params={"engine": "kokoro", "filtered": True},
        )
        # Without exposing the header, browsers can't read it cross-origin
        assert "X-Segment-Offsets-Ms" in resp.headers.get(
            "access-control-expose-headers", ""
        )

    def test_400_on_unknown_engine(self, client: TestClient) -> None:
        sid = _create_session(client)
        _wait_for_chunk_audio(client, sid, "c1")
        resp = client.get(
            f"/sessions/{sid}/chunks/c1/audio.variant",
            params={"engine": "bogus", "filtered": True},
        )
        assert resp.status_code == 400

    def test_404_when_narration_missing(self, client: TestClient) -> None:
        sid = _create_session(client)
        # Skip waiting; ask for a variant before narration exists
        resp = client.get(
            f"/sessions/{sid}/chunks/c99/audio.variant",
            params={"engine": "kokoro", "filtered": True},
        )
        assert resp.status_code in (404, 400)

    def test_filtered_and_raw_are_independent_variants(self, client: TestClient) -> None:
        sid = _create_session(client)
        _wait_for_chunk_audio(client, sid, "c1")

        filtered = client.get(
            f"/sessions/{sid}/chunks/c1/audio.variant",
            params={"engine": "kokoro", "filtered": True},
        )
        raw = client.get(
            f"/sessions/{sid}/chunks/c1/audio.variant",
            params={"engine": "kokoro", "filtered": False},
        )

        assert filtered.status_code == 200
        assert raw.status_code == 200

        # Both should now be cached
        cached = client.get(f"/sessions/{sid}/chunks/c1/audio/variants").json()["cached"]
        flat = {(c["engine"], c["filtered"]) for c in cached}
        assert ("kokoro", True) in flat
        assert ("kokoro", False) in flat


# ---------------------------------------------------------------------------
# /regenerate
# ---------------------------------------------------------------------------


class TestRegenerate:
    def test_wipes_narration_and_audio_and_re_kicks_worker(
        self, client: TestClient, in_memory_ctx
    ) -> None:
        sid = _create_session(client)
        _wait_for_chunk_audio(client, sid, "c1")

        # Sanity: things are cached
        assert in_memory_ctx.store.get_narration(sid, "c1") is not None
        assert in_memory_ctx.store.get_chunk_audio(sid, "c1") is not None

        # Touch a variant so we have something to wipe in audio_variants too
        client.get(
            f"/sessions/{sid}/chunks/c1/audio.variant",
            params={"engine": "kokoro", "filtered": True},
        )
        variants_before = in_memory_ctx.store.list_audio_variants(sid, "c1")
        assert variants_before  # at least one

        resp = client.post(f"/sessions/{sid}/chunks/c1/regenerate")
        assert resp.status_code == 200
        assert resp.json() == {"status": "regenerating", "chunk_id": "c1"}

        # Cache is gone immediately
        assert in_memory_ctx.store.list_audio_variants(sid, "c1") == []

        # The worker is kicked off — a follow-up /audio request long-polls
        # until the fresh audio lands
        audio_again = _wait_for_chunk_audio(client, sid, "c1")
        assert audio_again[:4] == b"RIFF"

    def test_404_on_unknown_chunk(self, client: TestClient) -> None:
        sid = _create_session(client)
        resp = client.post(f"/sessions/{sid}/chunks/c999/regenerate")
        assert resp.status_code == 404

    def test_404_on_unknown_session(self, client: TestClient) -> None:
        resp = client.post("/sessions/sess_nope/chunks/c1/regenerate")
        assert resp.status_code == 404
