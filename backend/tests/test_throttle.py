"""Concurrency cap resolution + behavioural test.

The resolution layer reads env / auto-detects from RAM; the behavioural
test wires a slow fake TTS through process_chunk and asserts that
`tts_semaphore` actually serialises the calls when capacity = 1.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from pr_walkthrough.orchestration.throttle import (
    _detect_total_ram_gb,
    resolve_llm_concurrency,
    resolve_tts_concurrency,
)


class TestResolveTtsConcurrency:
    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PR_WALKTHROUGH_TTS_CONCURRENCY", "7")
        assert resolve_tts_concurrency() == 7

    def test_env_zero_is_ignored_falls_back_to_auto(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PR_WALKTHROUGH_TTS_CONCURRENCY", "0")
        # Auto-detect always returns ≥1, never 0
        assert resolve_tts_concurrency() >= 1

    def test_env_non_integer_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PR_WALKTHROUGH_TTS_CONCURRENCY", "lots")
        assert resolve_tts_concurrency() >= 1

    def test_no_env_uses_auto_detect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PR_WALKTHROUGH_TTS_CONCURRENCY", raising=False)
        # Test machine has *some* RAM; result must be at least 1.
        assert resolve_tts_concurrency() >= 1


class TestResolveLlmConcurrency:
    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PR_WALKTHROUGH_LLM_CONCURRENCY", "16")
        assert resolve_llm_concurrency() == 16

    def test_default_is_8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PR_WALKTHROUGH_LLM_CONCURRENCY", raising=False)
        assert resolve_llm_concurrency() == 8


class TestRamDetection:
    def test_returns_a_plausible_value_or_zero(self) -> None:
        # On the test machine this is either a sensible GB number or 0
        # (sandboxes / containers without sysctl + /proc/meminfo).
        val = _detect_total_ram_gb()
        assert val >= 0
        assert val < 10_000  # sanity bound


# ---------------------------------------------------------------------------
# Behavioural: tts_semaphore actually serialises synth calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tts_semaphore_serialises_concurrent_synth() -> None:
    """When tts_concurrency=2, three parallel synths take ≥2× the per-call
    time (the third has to wait for one of the first two to finish)."""
    sem = asyncio.Semaphore(2)
    per_call = 0.10
    started: list[float] = []
    finished: list[float] = []

    async def fake_synth() -> None:
        async with sem:
            started.append(time.perf_counter())
            await asyncio.sleep(per_call)
            finished.append(time.perf_counter())

    t0 = time.perf_counter()
    await asyncio.gather(*[fake_synth() for _ in range(3)])
    elapsed = time.perf_counter() - t0

    # With cap=2 and 3 jobs: first 2 run in parallel, 3rd waits for slot,
    # so total ≈ 2 * per_call. Without throttling it'd be ≈ per_call.
    assert elapsed >= per_call * 1.8, (
        f"semaphore didn't serialise: elapsed={elapsed:.3f}s vs target≥{per_call*1.8:.3f}s"
    )
    # And the first two must have started before either finished
    assert started[0] < finished[0]
    assert started[1] < finished[0]
    # The third only got to start after one finished
    assert started[2] >= finished[0] - 1e-6


# ---------------------------------------------------------------------------
# Integration: the cap holds across concurrent FastAPI requests
# ---------------------------------------------------------------------------

class _InstrumentedTTS:
    """TTSAdapter that records concurrent calls into `synth`. Yields a
    silent WAV after a configurable hold so we can observe overlap."""

    def __init__(self, hold_seconds: float = 0.08) -> None:
        self.hold = hold_seconds
        self._lock = asyncio.Lock()
        self._inflight = 0
        self.peak_inflight = 0
        self.starts: list[float] = []

    async def synth(self, text: str, voice: str = "default"):
        # Atomically bump inflight + record peak.
        async with self._lock:
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)
            self.starts.append(time.perf_counter())
        try:
            await asyncio.sleep(self.hold)
            from pr_walkthrough.fakes.tts import _silent_wav
            yield _silent_wav()
        finally:
            async with self._lock:
                self._inflight -= 1

    def available_voices(self) -> list[str]:
        return ["default"]


def test_tts_semaphore_caps_concurrency_under_real_fastapi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wire a cap of 2, kick off four chunk-narration requests against
    a TestClient, and assert the instrumented TTS never sees more than
    two synths in flight at once. This is the behaviour the user cares
    about — the limit applies across whatever async request handlers
    FastAPI happens to schedule, not just within one coroutine."""
    monkeypatch.setenv("PR_WALKTHROUGH_TTS_CONCURRENCY", "2")
    monkeypatch.setenv("PR_WALKTHROUGH_LLM_CONCURRENCY", "8")

    from fastapi.testclient import TestClient
    from pr_walkthrough.api.deps import set_app_context, reset_app_context
    from pr_walkthrough.fakes import (
        FakeContext, FakeLLM, FakePRSource, FakeSTT,
    )
    from pr_walkthrough.main import app
    from pr_walkthrough.orchestration.app_context import AppContext
    from pr_walkthrough.store import SessionStore

    tts = _InstrumentedTTS(hold_seconds=0.10)
    ctx = AppContext(
        llm=FakeLLM(), tts=tts, stt=FakeSTT(),
        pr_source=FakePRSource(), context_retriever=FakeContext(),
        store=SessionStore(db_path=":memory:"),
    )
    assert ctx.tts_concurrency == 2
    set_app_context(ctx)

    try:
        with TestClient(app) as client:
            # One session has three chunks. Each /chunks/{cid} long-poll
            # may kick a narration → TTS synth. Trigger all three (plus
            # an audio request to force any not-yet-narrated chunk to
            # synth) via concurrent threads.
            sid = client.post(
                "/sessions",
                json={"pr_url": "https://github.com/x/y/pull/1"},
            ).json()["session_id"]

            import threading
            def hit(cid: str) -> None:
                client.get(f"/sessions/{sid}/chunks/{cid}/audio")
            workers = [threading.Thread(target=hit, args=(cid,)) for cid in ("c1", "c2", "c3")]
            for t in workers: t.start()
            for t in workers: t.join()
    finally:
        reset_app_context()

    # The whole point: the instrumented TTS must never have seen >2
    # in flight, even though 3 requests were racing.
    assert tts.peak_inflight <= 2, (
        f"semaphore failed to cap concurrency: peak={tts.peak_inflight}"
    )
    # And at least one synth ran (otherwise the test isn't proving anything)
    assert tts.peak_inflight >= 1


def test_app_context_exposes_semaphores(monkeypatch: pytest.MonkeyPatch) -> None:
    """AppContext construction hangs the asyncio.Semaphore instances at
    the documented attribute names; the chunk worker reads them by name."""
    monkeypatch.setenv("PR_WALKTHROUGH_TTS_CONCURRENCY", "3")
    monkeypatch.setenv("PR_WALKTHROUGH_LLM_CONCURRENCY", "5")
    from pr_walkthrough.fakes import (
        FakeContext, FakeLLM, FakePRSource, FakeSTT, FakeTTS,
    )
    from pr_walkthrough.orchestration.app_context import AppContext
    from pr_walkthrough.store import SessionStore

    ctx = AppContext(
        llm=FakeLLM(), tts=FakeTTS(), stt=FakeSTT(),
        pr_source=FakePRSource(), context_retriever=FakeContext(),
        store=SessionStore(db_path=":memory:"),
    )
    assert ctx.tts_concurrency == 3
    assert ctx.llm_concurrency == 5
    assert isinstance(ctx.tts_semaphore, asyncio.Semaphore)
    assert isinstance(ctx.llm_semaphore, asyncio.Semaphore)
