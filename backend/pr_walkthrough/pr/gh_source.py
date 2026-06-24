"""GhPRSource — implements PRSource by shelling out to the `gh` CLI.

Requires:
  - gh installed: https://cli.github.com/
  - gh auth login (or GITHUB_TOKEN env var)
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from typing import Any

from contracts.schemas import CodeAnchor, Hunk, PRMetadata

from .diff_parser import parse_unified_diff


# Matches https://github.com/{owner}/{repo}/pull/{number}
_PR_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


def _parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Return (owner, repo, number) from a GitHub PR URL."""
    m = _PR_URL_RE.match(pr_url.rstrip("/"))
    if not m:
        raise ValueError(
            f"Invalid GitHub PR URL: {pr_url!r}. "
            "Expected https://github.com/<owner>/<repo>/pull/<number>"
        )
    return m.group("owner"), m.group("repo"), int(m.group("number"))


async def _run_gh(*args: str, input: str | None = None) -> str:
    """Run a gh command and return stdout as a string.

    Raises RuntimeError with a clear message if:
      - gh is not installed
      - gh returns a non-zero exit code (includes auth errors)
    """
    gh_path = shutil.which("gh")
    if gh_path is None:
        raise RuntimeError(
            "gh CLI not found. Install from https://cli.github.com/ "
            "and run `gh auth login`."
        )

    stdin_pipe = asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        gh_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=stdin_pipe,
    )
    stdin_bytes = input.encode() if input is not None else None
    stdout, stderr = await proc.communicate(input=stdin_bytes)

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        # Surface auth hint
        if "not logged in" in err or "gh auth login" in err or "authentication" in err.lower():
            raise RuntimeError(
                f"gh auth login required. Run `gh auth login` first.\n{err}"
            )
        raise RuntimeError(
            f"`gh {' '.join(args)}` failed (exit {proc.returncode}):\n{err}"
        )

    return stdout.decode(errors="replace")


def _extract_repo_from_json(data: dict[str, Any]) -> str:
    """Build 'owner/name' from headRepository field."""
    hr = data.get("headRepository") or {}
    owner = (hr.get("owner") or {}).get("login") or ""
    name = hr.get("name") or ""
    if owner and name:
        return f"{owner}/{name}"
    # Fallback: parse from url
    url = data.get("url", "")
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/", url)
    if m:
        return m.group(1)
    return ""


class GhPRSource:
    """Fetches PR metadata + diffs and posts comments via the gh CLI."""

    # ------------------------------------------------------------------ #
    # PRSource protocol                                                    #
    # ------------------------------------------------------------------ #

    async def fetch(self, pr_url: str) -> tuple[PRMetadata, list[Hunk]]:
        """Fetch PR metadata and diff hunks."""
        metadata_task = asyncio.create_task(self._fetch_metadata(pr_url))
        diff_task = asyncio.create_task(self._fetch_diff(pr_url))
        metadata, hunks = await asyncio.gather(metadata_task, diff_task)
        return metadata, hunks

    async def post_comment(
        self,
        pr_url: str,
        body: str,
        anchor: CodeAnchor | None = None,
    ) -> str:
        """Post a PR comment; returns the URL of the new comment."""
        if anchor is None:
            return await self._post_general_comment(pr_url, body)
        return await self._post_inline_comment(pr_url, body, anchor)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _fetch_metadata(self, pr_url: str) -> PRMetadata:
        fields = "url,number,title,author,baseRefName,headRefName,baseRefOid,headRefOid,body,headRepository"
        raw = await _run_gh("pr", "view", pr_url, "--json", fields)
        data = json.loads(raw)

        # author can be a string or {"login": ...}
        author = data.get("author") or {}
        if isinstance(author, dict):
            author = author.get("login", "")

        repo = _extract_repo_from_json(data)

        return PRMetadata(
            url=data["url"],
            repo=repo,
            number=data["number"],
            title=data["title"],
            author=author,
            base_ref=data["baseRefName"],
            head_ref=data["headRefName"],
            base_sha=data.get("baseRefOid", ""),
            head_sha=data.get("headRefOid", ""),
            body=data.get("body") or "",
        )

    async def _fetch_diff(self, pr_url: str) -> list[Hunk]:
        raw = await _run_gh("pr", "diff", pr_url)
        return parse_unified_diff(raw)

    async def _post_general_comment(self, pr_url: str, body: str) -> str:
        """gh pr comment <url> --body <body>  -- returns comment URL."""
        raw = await _run_gh("pr", "comment", pr_url, "--body", body)
        # gh outputs something like:
        #   https://github.com/owner/repo/pull/123#issuecomment-456
        url = raw.strip()
        return url

    async def _post_inline_comment(
        self, pr_url: str, body: str, anchor: CodeAnchor
    ) -> str:
        """Post an inline review comment using gh api."""
        owner, repo, number = _parse_pr_url(pr_url)

        # Fetch the head SHA for the commit_id parameter
        fields = "headRefOid"
        raw = await _run_gh("pr", "view", pr_url, "--json", fields)
        data = json.loads(raw)
        commit_id = data["headRefOid"]

        start_line, end_line = anchor.line_range
        payload: dict[str, Any] = {
            "body": body,
            "commit_id": commit_id,
            "path": anchor.file,
            "line": end_line,
            "side": "RIGHT",
        }
        if start_line != end_line:
            payload["start_line"] = start_line
            payload["start_side"] = "RIGHT"

        raw_response = await _run_gh(
            "api",
            f"repos/{owner}/{repo}/pulls/{number}/comments",
            "--method", "POST",
            "--input", "-",
            input=json.dumps(payload),
        )
        resp = json.loads(raw_response)
        return resp.get("html_url", "")
