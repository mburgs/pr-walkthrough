"""CLI for the LLM adapter — Stream 3.

Usage (from backend/ directory):

    # Real Claude (requires ANTHROPIC_API_KEY):
    python -m pr_walkthrough.llm.cli plan /path/to/fixtures/pr_small

    # Mock mode (prints fixture data verbatim, no API key needed):
    python -m pr_walkthrough.llm.cli --mock plan /path/to/fixtures/pr_small
    python -m pr_walkthrough.llm.cli --mock narrate /path/to/fixtures/pr_small c1
    python -m pr_walkthrough.llm.cli --mock answer /path/to/fixtures/pr_small c2 "Is rotate() atomic?"

Subcommands
-----------
    plan <fixture_dir>
        Reads metadata.json + diff.json, calls plan_tour, prints TourPlan as JSON.

    narrate <fixture_dir> <chunk_id>
        Reads tour_plan.json + chunks/<chunk_id>.narration.json (for mock),
        calls narrate_chunk with the specified chunk from the tour plan,
        prints ChunkNarration as JSON.

    answer <fixture_dir> <chunk_id> <question>
        Reads tour_plan.json + chunks/<chunk_id>.narration.json (for mock),
        calls answer_follow_up with the question as a FollowUp,
        prints FollowUpAnswer as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure contracts and backend packages are on sys.path when run directly
_repo_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
_backend_root = Path(__file__).resolve().parent.parent.parent
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from contracts.schemas import (
    ChunkNarration,
    FollowUp,
    Hunk,
    PRMetadata,
    TourPlan,
)
from pr_walkthrough.llm.adapter import ClaudeLLMAdapter


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------

def load_pr_metadata(fixture_dir: Path) -> PRMetadata:
    data = json.loads((fixture_dir / "metadata.json").read_text())
    return PRMetadata.model_validate(data)


def load_diff(fixture_dir: Path) -> list[Hunk]:
    data = json.loads((fixture_dir / "diff.json").read_text())
    return [Hunk.model_validate(h) for h in data]


def load_tour_plan(fixture_dir: Path) -> TourPlan:
    data = json.loads((fixture_dir / "tour_plan.json").read_text())
    return TourPlan.model_validate(data)


def load_chunk_narration(fixture_dir: Path, chunk_id: str) -> ChunkNarration:
    path = fixture_dir / "chunks" / f"{chunk_id}.narration.json"
    data = json.loads(path.read_text())
    return ChunkNarration.model_validate(data)


def load_follow_up_example(fixture_dir: Path):
    path = fixture_dir / "follow_up_example.json"
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

async def cmd_plan(fixture_dir: Path, mock: bool) -> None:
    if mock:
        plan = load_tour_plan(fixture_dir)
        print(plan.model_dump_json(indent=2))
        return

    pr = load_pr_metadata(fixture_dir)
    diff = load_diff(fixture_dir)
    adapter = ClaudeLLMAdapter()
    plan = await adapter.plan_tour(pr, diff)
    print(plan.model_dump_json(indent=2))


async def cmd_narrate(fixture_dir: Path, chunk_id: str, mock: bool) -> None:
    if mock:
        narration = load_chunk_narration(fixture_dir, chunk_id)
        print(narration.model_dump_json(indent=2))
        return

    plan = load_tour_plan(fixture_dir)
    chunk = next((c for c in plan.chunks if c.chunk_id == chunk_id), None)
    if chunk is None:
        print(
            f"ERROR: chunk_id '{chunk_id}' not found in tour_plan. "
            f"Available: {[c.chunk_id for c in plan.chunks]}",
            file=sys.stderr,
        )
        sys.exit(1)

    adapter = ClaudeLLMAdapter()
    narration = await adapter.narrate_chunk(plan, chunk, related=[])
    print(narration.model_dump_json(indent=2))


async def cmd_answer(
    fixture_dir: Path,
    chunk_id: str,
    question: str,
    mock: bool,
) -> None:
    if mock:
        example = load_follow_up_example(fixture_dir)
        from contracts.schemas import FollowUpAnswer
        answer = FollowUpAnswer.model_validate(example["answer"])
        print(answer.model_dump_json(indent=2))
        return

    plan = load_tour_plan(fixture_dir)
    # Gather history: all narration chunks before and including chunk_id
    history: list[ChunkNarration] = []
    chunk_ids_in_plan = [c.chunk_id for c in plan.chunks]
    if chunk_id in chunk_ids_in_plan:
        idx = chunk_ids_in_plan.index(chunk_id)
        narrated_ids = chunk_ids_in_plan[: idx + 1]
        chunks_dir = fixture_dir / "chunks"
        for cid in narrated_ids:
            narration_path = chunks_dir / f"{cid}.narration.json"
            if narration_path.exists():
                history.append(load_chunk_narration(fixture_dir, cid))

    follow_up = FollowUp(chunk_id=chunk_id, question_text=question)
    adapter = ClaudeLLMAdapter()
    answer = await adapter.answer_follow_up(plan, history, follow_up)
    print(answer.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="pr-walkthrough LLM adapter CLI (Stream 3)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Return fixture data verbatim without calling Claude (no API key needed)",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_plan = subparsers.add_parser("plan", help="Generate a tour plan from a fixture dir")
    p_plan.add_argument("fixture_dir", type=Path, help="Path to fixture directory")

    p_narrate = subparsers.add_parser("narrate", help="Narrate a single chunk")
    p_narrate.add_argument("fixture_dir", type=Path, help="Path to fixture directory")
    p_narrate.add_argument("chunk_id", help="Chunk ID to narrate (e.g. c1)")

    p_answer = subparsers.add_parser("answer", help="Answer a follow-up question")
    p_answer.add_argument("fixture_dir", type=Path, help="Path to fixture directory")
    p_answer.add_argument("chunk_id", help="Current chunk ID context")
    p_answer.add_argument("question", help="The reviewer's question")

    args = parser.parse_args()

    # Check API key for non-mock real calls
    if not args.mock and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY is not set. "
            "Use --mock for offline mode or set the env var.",
            file=sys.stderr,
        )
        sys.exit(1)

    fixture_dir = Path(args.fixture_dir).resolve()
    if not fixture_dir.is_dir():
        print(f"ERROR: fixture_dir does not exist: {fixture_dir}", file=sys.stderr)
        sys.exit(1)

    if args.subcommand == "plan":
        asyncio.run(cmd_plan(fixture_dir, args.mock))
    elif args.subcommand == "narrate":
        asyncio.run(cmd_narrate(fixture_dir, args.chunk_id, args.mock))
    elif args.subcommand == "answer":
        asyncio.run(cmd_answer(fixture_dir, args.chunk_id, args.question, args.mock))


if __name__ == "__main__":
    main()
