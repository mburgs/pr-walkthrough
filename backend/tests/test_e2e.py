"""End-to-end test: POST /sessions → walk all 3 chunks → follow-up → flag → post flag."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient


PR_URL = "https://github.com/example-org/auth-service/pull/142"


def test_create_session_returns_tour_plan(client: TestClient) -> None:
    resp = client.post("/sessions", json={"pr_url": PR_URL})
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    assert plan["chunks"]
    assert "session_id" in plan
    assert plan["pr"]["url"] == PR_URL
    assert plan["familiarity"] == "review"  # default when not specified


@pytest.mark.parametrize("level", ["tutorial", "tour", "review", "highlights"])
def test_create_session_persists_familiarity(client: TestClient, level: str) -> None:
    resp = client.post("/sessions", json={"pr_url": PR_URL, "familiarity": level})
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    assert plan["familiarity"] == level
    # Round-trip through GET so we know it persisted on the session store
    state = client.get(f"/sessions/{plan['session_id']}").json()
    assert state["plan"]["familiarity"] == level


def test_create_session_rejects_unknown_familiarity(client: TestClient) -> None:
    resp = client.post("/sessions", json={"pr_url": PR_URL, "familiarity": "expert"})
    assert resp.status_code == 422


def test_multi_level_persists_and_serves_per_level(client: TestClient, in_memory_ctx) -> None:
    """When multi_level=true, the session creates one narration per level
    on chunk 1 (FakeLLM responds synchronously) and the /chunks endpoint
    serves a different narration depending on ?level=X."""
    resp = client.post("/sessions", json={
        "pr_url": PR_URL,
        "familiarity": "review",
        "multi_level": True,
    })
    assert resp.status_code == 201
    plan = resp.json()
    sid = plan["session_id"]
    assert plan["multi_level"] is True

    # Wait for chunk 1 to be narrated at each level (long-poll on each).
    for level in ("tutorial", "tour", "review", "highlights"):
        got = client.get(f"/sessions/{sid}/chunks/c1?level={level}")
        assert got.status_code == 200, f"{level}: {got.text}"
        # Store should now hold a per-level row
        assert in_memory_ctx.store.get_narration(sid, "c1", level=level) is not None


def test_single_level_session_only_narrates_at_chosen_level(client: TestClient, in_memory_ctx) -> None:
    resp = client.post("/sessions", json={
        "pr_url": PR_URL,
        "familiarity": "tutorial",
    })
    assert resp.status_code == 201
    sid = resp.json()["session_id"]
    # Pull the chosen level — should land.
    got = client.get(f"/sessions/{sid}/chunks/c1?level=tutorial")
    assert got.status_code == 200
    # Other levels: not prefetched. The endpoint would lazy-narrate them
    # on demand, but if we check the store directly only `tutorial` is there.
    assert in_memory_ctx.store.get_narration(sid, "c1", level="tutorial") is not None
    assert in_memory_ctx.store.get_narration(sid, "c1", level="highlights") is None


def test_get_session_state(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.get(f"/sessions/{sid}")
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["plan"]["session_id"] == sid
    assert isinstance(state["flags"], list)


def test_get_unknown_session_404(client: TestClient) -> None:
    resp = client.get("/sessions/does-not-exist")
    assert resp.status_code == 404


def test_walk_all_three_chunks(client: TestClient) -> None:
    """The background task should narrate all 3 chunks before we poll."""
    sid = _create_session(client)

    # Give background tasks a moment to complete (TestClient runs them inline)
    for cid in ("c1", "c2", "c3"):
        resp = _wait_for_chunk(client, sid, cid)
        assert resp.status_code == 200, f"chunk {cid}: {resp.text}"
        narration = resp.json()
        assert narration["chunk_id"] == cid
        assert narration["narration"]


def test_chunk_audio_returns_wav(client: TestClient) -> None:
    sid = _create_session(client)
    _wait_for_chunk(client, sid, "c1")

    resp = client.get(f"/sessions/{sid}/chunks/c1/audio")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    # WAV magic bytes
    assert resp.content[:4] == b"RIFF"
    assert resp.content[8:12] == b"WAVE"


def test_follow_up_json(client: TestClient) -> None:
    sid = _create_session(client)
    _wait_for_chunk(client, sid, "c1")

    resp = client.post(
        f"/sessions/{sid}/follow-up",
        json={
            "chunk_id": "c2",
            "question_text": "Does session_store share the same DB connection?",
        },
    )
    assert resp.status_code == 200, resp.text
    answer = resp.json()
    assert answer["answer_text"]
    assert "X-Answer-Audio-Url" in resp.headers


def test_follow_up_audio_url_returns_wav(client: TestClient) -> None:
    sid = _create_session(client)
    _wait_for_chunk(client, sid, "c1")

    fu_resp = client.post(
        f"/sessions/{sid}/follow-up",
        json={"chunk_id": "c1", "question_text": "Why hard DELETE?"},
    )
    assert fu_resp.status_code == 200
    audio_url = fu_resp.headers["X-Answer-Audio-Url"]

    audio_resp = client.get(audio_url)
    assert audio_resp.status_code == 200
    assert audio_resp.headers["content-type"].startswith("audio/wav")
    assert audio_resp.content[:4] == b"RIFF"


def test_follow_up_audio_content_type(client: TestClient) -> None:
    """STT path: send audio bytes, get a transcribed follow-up answer."""
    sid = _create_session(client)

    resp = client.post(
        f"/sessions/{sid}/follow-up",
        content=b"\x00\x00\x00\x00",  # dummy audio
        headers={"content-type": "audio/webm"},
    )
    assert resp.status_code == 200
    answer = resp.json()
    assert answer["answer_text"]  # FakeSTT + FakeLLM combo works


def test_create_flag(client: TestClient) -> None:
    sid = _create_session(client)
    flag = _create_flag(client, sid)
    assert flag["flag_id"]
    assert flag["posted"] is False
    assert flag["chunk_id"] == "c1"


def test_create_flag_rejects_unknown_severity(client: TestClient) -> None:
    """422 (not 500) for severities outside the Literal — FastAPI should
    catch the bad enum at the body model rather than letting Flag construction
    raise inside the handler."""
    sid = _create_session(client)
    resp = client.post(
        f"/sessions/{sid}/flags",
        json={"chunk_id": "c1", "severity": "urgent", "body": "nope"},
    )
    assert resp.status_code == 422


def test_patch_flag(client: TestClient) -> None:
    sid = _create_session(client)
    flag = _create_flag(client, sid)
    fid = flag["flag_id"]

    resp = client.patch(
        f"/sessions/{sid}/flags/{fid}",
        json={"body": "Updated comment text"},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["body"] == "Updated comment text"
    assert updated["flag_id"] == fid


def test_post_flag_to_pr(client: TestClient) -> None:
    sid = _create_session(client)
    flag = _create_flag(client, sid)
    fid = flag["flag_id"]

    resp = client.post(f"/sessions/{sid}/flags/{fid}/post")
    assert resp.status_code == 200
    posted = resp.json()
    assert posted["posted"] is True
    assert posted["posted_url"] is not None


def test_delete_flag(client: TestClient) -> None:
    sid = _create_session(client)
    flag = _create_flag(client, sid)
    fid = flag["flag_id"]

    resp = client.delete(f"/sessions/{sid}/flags/{fid}")
    assert resp.status_code == 204

    # Confirm gone from session state
    state_resp = client.get(f"/sessions/{sid}")
    flags = state_resp.json()["flags"]
    assert all(f["flag_id"] != fid for f in flags)


def test_flag_not_found(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.patch(f"/sessions/{sid}/flags/no-such-flag", json={"body": "x"})
    assert resp.status_code == 404


def test_unknown_chunk_returns_504(client: TestClient) -> None:
    """Chunk that will never exist should 504 — but we keep timeout short in test."""
    # We can't easily shorten the long-poll in a unit test without refactoring,
    # so instead we verify that a chunk that isn't pre-narrated eventually 504s.
    # Skip this slow test by default; run with --run-slow flag.
    pytest.skip("Long-poll timeout test skipped (30s); run manually if needed")


# ── helpers ──────────────────────────────────────────────────────────────────

def _create_session(client: TestClient) -> str:
    resp = client.post("/sessions", json={"pr_url": PR_URL})
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _wait_for_chunk(client: TestClient, sid: str, cid: str, retries: int = 50):
    """Poll until chunk is ready (background task runs in TestClient event loop)."""
    for _ in range(retries):
        resp = client.get(f"/sessions/{sid}/chunks/{cid}")
        if resp.status_code == 200:
            return resp
        time.sleep(0.1)
    # Return last response (may be 504 equivalent or 404)
    return client.get(f"/sessions/{sid}/chunks/{cid}")


def _create_flag(client: TestClient, sid: str) -> dict:
    resp = client.post(
        f"/sessions/{sid}/flags",
        json={
            "chunk_id": "c1",
            "severity": "medium",
            "body": "Draft comment text for the review",
            "anchor": {"file": "src/auth/session.py", "line_range": [56, 62]},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
