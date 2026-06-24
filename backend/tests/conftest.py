"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pr_walkthrough.fakes import FakeLLM, FakePRSource, FakeSTT, FakeTTS, FakeContext
from pr_walkthrough.main import app
from pr_walkthrough.api.deps import set_app_context
from pr_walkthrough.orchestration import AppContext
from pr_walkthrough.store import SessionStore


@pytest.fixture()
def in_memory_ctx() -> AppContext:
    """AppContext with fakes for all adapters + in-memory SQLite (isolated per test).

    Production wiring uses real Claude / Whisper / Kokoro / gh; tests use the
    fakes explicitly so the suite stays fast and offline.
    """
    ctx = AppContext(
        llm=FakeLLM(),
        tts=FakeTTS(),
        stt=FakeSTT(),
        pr_source=FakePRSource(),
        context_retriever=FakeContext(),
        store=SessionStore(db_path=":memory:"),
    )
    set_app_context(ctx)
    return ctx


@pytest.fixture()
def client(in_memory_ctx) -> TestClient:
    """Sync test client.  Background tasks run inline via anyio."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
