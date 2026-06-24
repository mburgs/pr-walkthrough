"""FakePRSource — returns pr_small fixture data for any PR URL."""

from __future__ import annotations

import json
from pathlib import Path

from contracts.schemas import CodeAnchor, Hunk, PRMetadata

_FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures" / "pr_small"


class FakePRSource:
    """Satisfies PRSource protocol by reading pr_small fixtures."""

    async def fetch(self, pr_url: str) -> tuple[PRMetadata, list[Hunk]]:
        meta_raw = json.loads((_FIXTURES / "metadata.json").read_text())
        # Override URL to match the requested one so session stores the right url
        meta_raw["url"] = pr_url
        metadata = PRMetadata.model_validate(meta_raw)

        diff_raw = json.loads((_FIXTURES / "diff.json").read_text())
        hunks = [Hunk.model_validate(h) for h in diff_raw]

        return metadata, hunks

    async def post_comment(
        self,
        pr_url: str,
        body: str,
        anchor: CodeAnchor | None = None,
    ) -> str:
        """Fake post — returns a synthetic comment URL."""
        return f"https://github.com/example-org/auth-service/pull/142#issuecomment-fake-{id(body)}"
