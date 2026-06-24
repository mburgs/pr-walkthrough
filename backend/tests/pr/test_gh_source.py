"""Tests for GhPRSource — mock-gh tests verifying exact gh args.

All subprocess calls are intercepted via monkeypatching
asyncio.create_subprocess_exec.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from contracts.schemas import CodeAnchor
from pr_walkthrough.pr.gh_source import GhPRSource, _parse_pr_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_PR_URL = "https://github.com/test-owner/test-repo/pull/42"

_FAKE_VIEW_JSON = {
    "url": _FAKE_PR_URL,
    "number": 42,
    "title": "Test PR",
    "author": {"login": "alice"},
    "baseRefName": "main",
    "headRefName": "feat/test",
    "baseRefOid": "abc123",
    "headRefOid": "def456",
    "body": "PR body",
    "headRepository": {
        "name": "test-repo",
        "owner": {"login": "test-owner"},
    },
}

_FAKE_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,2 +1,3 @@ def bar:\n"
    " ctx\n"
    "+new\n"
    " ctx2\n"
)


def _make_mock_proc(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(
        return_value=(stdout.encode(), stderr.encode())
    )
    return proc


class _GhCallTracker:
    """Captures each call to create_subprocess_exec and returns preset responses.

    Must be used as an AsyncMock side_effect so the coroutine is awaited properly.
    """

    def __init__(self, responses: list[tuple[str, int]]) -> None:
        # responses: list of (stdout, returncode) in call order
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []
        self._idx = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> MagicMock:
        self.calls.append(args)
        stdout, rc = self.responses[self._idx]
        self._idx += 1
        return _make_mock_proc(stdout, rc)


def _async_side_effect(tracker: _GhCallTracker) -> AsyncMock:
    """Wrap tracker so AsyncMock uses it as the side_effect."""
    mock = AsyncMock(side_effect=tracker)
    return mock


# ---------------------------------------------------------------------------
# _parse_pr_url
# ---------------------------------------------------------------------------


class TestParsePrUrl:
    def test_valid_url(self) -> None:
        owner, repo, number = _parse_pr_url("https://github.com/cli/cli/pull/9999")
        assert owner == "cli"
        assert repo == "cli"
        assert number == 9999

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            _parse_pr_url("https://gitlab.com/foo/bar/merge_requests/1")

    def test_trailing_slash(self) -> None:
        owner, repo, number = _parse_pr_url(
            "https://github.com/owner/my-repo/pull/1/"
        )
        assert owner == "owner"
        assert repo == "my-repo"
        assert number == 1


# ---------------------------------------------------------------------------
# GhPRSource.fetch — verifies gh args
# ---------------------------------------------------------------------------


class TestGhFetch:
    @pytest.mark.asyncio
    async def test_fetch_calls_correct_gh_commands(self) -> None:
        tracker = _GhCallTracker(
            responses=[
                (json.dumps(_FAKE_VIEW_JSON), 0),  # pr view
                (_FAKE_DIFF, 0),                   # pr diff
            ]
        )

        with patch("pr_walkthrough.pr.gh_source.shutil.which", return_value="/usr/bin/gh"):
            with patch("asyncio.create_subprocess_exec", side_effect=tracker):
                source = GhPRSource()
                metadata, hunks = await source.fetch(_FAKE_PR_URL)

        assert len(tracker.calls) == 2

        # First call: gh pr view <url> --json <fields>
        view_args = tracker.calls[0]
        assert view_args[0].endswith("gh")
        assert "pr" in view_args
        assert "view" in view_args
        assert _FAKE_PR_URL in view_args
        assert "--json" in view_args

        # Second call: gh pr diff <url>
        diff_args = tracker.calls[1]
        assert diff_args[0].endswith("gh")
        assert "pr" in diff_args
        assert "diff" in diff_args
        assert _FAKE_PR_URL in diff_args

        # Check metadata shape
        assert metadata.url == _FAKE_PR_URL
        assert metadata.number == 42
        assert metadata.repo == "test-owner/test-repo"
        assert metadata.author == "alice"
        assert metadata.base_sha == "abc123"
        assert metadata.head_sha == "def456"

        # Check diff parsed
        assert len(hunks) == 1
        assert hunks[0].file == "foo.py"
        assert hunks[0].old_range == (1, 2)
        assert hunks[0].new_range == (1, 3)

    @pytest.mark.asyncio
    async def test_gh_not_found_raises_clear_error(self) -> None:
        with patch("pr_walkthrough.pr.gh_source.shutil.which", return_value=None):
            source = GhPRSource()
            with pytest.raises(RuntimeError, match="gh CLI not found"):
                await source.fetch(_FAKE_PR_URL)

    @pytest.mark.asyncio
    async def test_gh_auth_error_raises_clear_error(self) -> None:
        proc = _make_mock_proc(
            stdout="",
            returncode=1,
            stderr="not logged in. run `gh auth login`",
        )

        with patch("pr_walkthrough.pr.gh_source.shutil.which", return_value="/usr/bin/gh"):
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                source = GhPRSource()
                with pytest.raises(RuntimeError, match="gh auth login required"):
                    await source.fetch(_FAKE_PR_URL)


# ---------------------------------------------------------------------------
# GhPRSource.post_comment — verifies gh args
# ---------------------------------------------------------------------------


class TestGhPostComment:
    @pytest.mark.asyncio
    async def test_general_comment_uses_pr_comment(self) -> None:
        tracker = _GhCallTracker(
            responses=[
                (f"{_FAKE_PR_URL}#issuecomment-999\n", 0),
            ]
        )

        with patch("pr_walkthrough.pr.gh_source.shutil.which", return_value="/usr/bin/gh"):
            with patch("asyncio.create_subprocess_exec", side_effect=tracker):
                source = GhPRSource()
                url = await source.post_comment(_FAKE_PR_URL, "hello world")

        assert len(tracker.calls) == 1
        args = tracker.calls[0]
        assert "pr" in args
        assert "comment" in args
        assert _FAKE_PR_URL in args
        assert "--body" in args
        assert "hello world" in args
        assert "issuecomment-999" in url

    @pytest.mark.asyncio
    async def test_inline_comment_uses_gh_api(self) -> None:
        view_response = json.dumps({"headRefOid": "def456"})
        api_response = json.dumps({
            "html_url": f"{_FAKE_PR_URL}#discussion_r123"
        })

        tracker = _GhCallTracker(
            responses=[
                (view_response, 0),  # pr view for headRefOid
                (api_response, 0),   # api POST
            ]
        )

        anchor = CodeAnchor(file="src/foo.py", line_range=(10, 15))

        with patch("pr_walkthrough.pr.gh_source.shutil.which", return_value="/usr/bin/gh"):
            with patch("asyncio.create_subprocess_exec", side_effect=tracker):
                source = GhPRSource()
                url = await source.post_comment(_FAKE_PR_URL, "nice catch", anchor)

        assert len(tracker.calls) == 2

        # First call: pr view to get headRefOid
        view_args = tracker.calls[0]
        assert "pr" in view_args
        assert "view" in view_args

        # Second call: gh api repos/.../pulls/.../comments
        api_args = tracker.calls[1]
        assert "api" in api_args
        assert any("pulls/42/comments" in str(a) for a in api_args)
        assert "--method" in api_args
        assert "POST" in api_args

        assert "discussion_r123" in url

    @pytest.mark.asyncio
    async def test_inline_comment_single_line_no_start_line(self) -> None:
        """Single-line anchor must NOT include start_line in payload."""
        view_response = json.dumps({"headRefOid": "abc"})

        captured_input: list[str] = []

        async def fake_exec(*args: Any, **kwargs: Any) -> MagicMock:
            if "--input" in args:
                # next call: capture what's piped in via communicate
                pass
            proc = MagicMock()
            proc.returncode = 0
            # Capture the input bytes that will be written
            async def communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
                if input:
                    captured_input.append(input.decode())
                return (json.dumps({"html_url": "https://example.com/r1"}).encode(), b"")
            proc.communicate = communicate
            return proc

        anchor = CodeAnchor(file="foo.py", line_range=(5, 5))

        with patch("pr_walkthrough.pr.gh_source.shutil.which", return_value="/usr/bin/gh"):
            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                source = GhPRSource()
                await source.post_comment(_FAKE_PR_URL, "single line", anchor)

        if captured_input:
            payload = json.loads(captured_input[-1])
            assert "start_line" not in payload
            assert payload["line"] == 5


# ---------------------------------------------------------------------------
# Live test (skipped unless GH_LIVE_TEST_PR is set)
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestGhSourceLive:
    """Hits a real public PR. Requires `gh auth login` and GH_LIVE_TEST_PR env var."""

    @pytest.mark.asyncio
    async def test_fetch_real_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os
        pr_url = os.environ.get("GH_LIVE_TEST_PR")
        if not pr_url:
            pytest.skip("GH_LIVE_TEST_PR not set")

        source = GhPRSource()
        metadata, hunks = await source.fetch(pr_url)

        assert metadata.url == pr_url or pr_url in metadata.url
        assert metadata.number > 0
        assert metadata.repo  # non-empty
        assert isinstance(hunks, list)
        # Every hunk must have a non-empty file and a body
        for h in hunks:
            assert h.file
            assert h.body is not None
