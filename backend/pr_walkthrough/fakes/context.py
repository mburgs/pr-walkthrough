"""FakeContext — lifts related_code from fixture narrations."""

from __future__ import annotations

import json
from pathlib import Path

from contracts.schemas import CodeAnchor, RelatedCode

_FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures" / "pr_small"


class FakeContext:
    """Satisfies ContextRetriever protocol. Returns related_code from fixtures."""

    # Cache of chunk_id -> list[RelatedCode] built at first access
    _cache: dict[str, list[RelatedCode]] | None = None

    def _build_cache(self) -> dict[str, list[RelatedCode]]:
        cache: dict[str, list[RelatedCode]] = {}
        chunks_dir = _FIXTURES / "chunks"
        for path in sorted(chunks_dir.glob("*.narration.json")):
            raw = json.loads(path.read_text())
            chunk_id = raw.get("chunk_id", path.stem.split(".")[0])
            cache[chunk_id] = [
                RelatedCode.model_validate(rc) for rc in raw.get("related_code", [])
            ]
        return cache

    async def related(
        self, anchor: CodeAnchor, repo_root: Path
    ) -> list[RelatedCode]:
        if self._cache is None:
            self._cache = self._build_cache()
        # Return all related code from all chunks (union, deduplicated by anchor+relationship)
        seen: set[tuple[str, int, int, str]] = set()
        result: list[RelatedCode] = []
        for items in self._cache.values():
            for item in items:
                key = (
                    item.anchor.file,
                    item.anchor.line_range[0],
                    item.anchor.line_range[1],
                    item.relationship,
                )
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return result
