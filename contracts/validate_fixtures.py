"""Load every fixture file and parse it against its contract.

Run: `python -m contracts.validate_fixtures`
Exits non-zero if any fixture fails to validate. CI gate against contract drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel, TypeAdapter, ValidationError

from contracts.schemas import (
    ChunkNarration,
    Flag,
    FollowUp,
    FollowUpAnswer,
    Hunk,
    PRMetadata,
    TourPlan,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures"


def _check(path: Path, model: type[BaseModel] | TypeAdapter) -> str | None:
    try:
        data = json.loads(path.read_text())
        if isinstance(model, TypeAdapter):
            model.validate_python(data)
        else:
            model.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        return f"{path.relative_to(REPO_ROOT)}: {e}"
    return None


def main() -> int:
    hunk_list = TypeAdapter(list[Hunk])
    flag_list = TypeAdapter(list[Flag])

    checks: list[tuple[Path, type[BaseModel] | TypeAdapter]] = []

    pr_small = FIXTURES / "pr_small"
    checks.append((pr_small / "metadata.json", PRMetadata))
    checks.append((pr_small / "diff.json", hunk_list))
    checks.append((pr_small / "tour_plan.json", TourPlan))
    checks.append((pr_small / "flags_example.json", flag_list))
    for chunk in sorted((pr_small / "chunks").glob("*.narration.json")):
        checks.append((chunk, ChunkNarration))

    # follow-up has a composite shape; validate the parts
    fu_path = pr_small / "follow_up_example.json"
    fu_data = json.loads(fu_path.read_text())
    try:
        FollowUp.model_validate(fu_data["follow_up"])
        FollowUpAnswer.model_validate(fu_data["answer"])
    except (KeyError, ValidationError) as e:
        print(f"FAIL {fu_path.relative_to(REPO_ROOT)}: {e}", file=sys.stderr)
        return 1

    failures = [err for err in (_check(p, m) for p, m in checks) if err]
    for err in failures:
        print(f"FAIL {err}", file=sys.stderr)

    if failures:
        return 1

    total = len(checks) + 1  # +1 for follow-up
    print(f"OK — {total} fixtures validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
