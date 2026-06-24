"""M1 walking skeleton: real PR → real Claude → terminal-printed walkthrough.

The integration point where streams 3 (LLM) and 6 (gh PR source) land into
stream 2 (orchestrator). No UI, no TTS — just a CLI that proves the prompt
chain works against real PRs.

Run:
    python -m pr_walkthrough.m1 <pr-url>             # real Claude + real gh
    python -m pr_walkthrough.m1 <pr-url> --mock      # fakes; works offline
    python -m pr_walkthrough.m1 <pr-url> --plan-only # skip per-chunk narration

Requires (default mode):
    - ANTHROPIC_API_KEY in env
    - gh CLI installed and authenticated
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import textwrap
from pathlib import Path

from contracts.adapters import LLMAdapter, PRSource
from contracts.schemas import ChunkNarration, TourChunk, TourPlan


def _build_adapters(mock: bool) -> tuple[PRSource, LLMAdapter]:
    if mock:
        from pr_walkthrough.fakes import FakeLLM, FakePRSource

        return FakePRSource(), FakeLLM()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY not set. Either export it, or rerun with --mock."
        )
    from pr_walkthrough.llm.adapter import ClaudeLLMAdapter
    from pr_walkthrough.pr.gh_source import GhPRSource

    return GhPRSource(), ClaudeLLMAdapter()


def _print_header(plan: TourPlan) -> None:
    pr = plan.pr
    files = sorted({h.file for c in plan.chunks for h in c.hunks})
    print()
    print(f"PR: {pr.repo}#{pr.number} — {pr.title}")
    print(f"Author: {pr.author}   {pr.base_ref} ← {pr.head_ref}")
    print(f"Files changed: {len(files)}")
    print()


def _print_plan(plan: TourPlan) -> None:
    bar = "─" * 60
    print(f"┌─ TOUR PLAN ({len(plan.chunks)} chunks) {bar[:46]}")
    for chunk in plan.chunks:
        files = ", ".join(chunk.files)
        print(f"│")
        print(f"│  [{chunk.chunk_id}] {files}")
        print(f"│       concern: {chunk.est_concern_level}")
        for line in textwrap.wrap(chunk.summary, width=72):
            print(f"│       {line}")
        for line in textwrap.wrap(
            f"why here: {chunk.rationale_for_position}", width=72
        ):
            print(f"│       {line}")
    print("│")
    print(f"└{'─' * 60}")
    print()


def _print_narration(chunk: TourChunk, narration: ChunkNarration) -> None:
    bar = "═" * 60
    print(f"{bar}")
    print(f"  CHUNK {chunk.chunk_id} — {', '.join(chunk.files)}")
    print(f"{bar}")
    print()
    for para in narration.narration.split("\n\n"):
        for line in textwrap.wrap(para, width=78):
            print(f"  {line}")
        print()

    if narration.highlights:
        print("  Highlights:")
        for h in narration.highlights:
            a = h.anchor
            print(f"    {a.file}:{a.line_range[0]}-{a.line_range[1]}  {h.why}")
        print()

    if narration.concerns:
        print("  Concerns:")
        for c in narration.concerns:
            print(f"    [{c.severity}] {c.text}")
            if c.suggested_question:
                print(f"         → {c.suggested_question}")
        print()

    if narration.look_closer_for:
        print("  Look closer:")
        for item in narration.look_closer_for:
            print(f"    • {item}")
        print()


async def _amain(pr_url: str, mock: bool, plan_only: bool) -> int:
    pr_source, llm = _build_adapters(mock)

    print(f"Fetching {pr_url} ...", file=sys.stderr)
    metadata, hunks = await pr_source.fetch(pr_url)

    print(f"Planning tour ({len(hunks)} hunks) ...", file=sys.stderr)
    plan = await llm.plan_tour(metadata, hunks)

    _print_header(plan)
    _print_plan(plan)

    if plan_only:
        return 0

    for chunk in plan.chunks:
        print(f"Narrating {chunk.chunk_id} ...", file=sys.stderr)
        # Pass empty related-code list for M1; ContextRetriever lands in M5.
        narration = await llm.narrate_chunk(plan, chunk, related=[])
        _print_narration(chunk, narration)

    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="pr-walkthrough-m1", description=__doc__.splitlines()[0])
    p.add_argument("pr_url", help="GitHub PR URL")
    p.add_argument("--mock", action="store_true", help="use fakes (no API key, no gh)")
    p.add_argument(
        "--plan-only",
        action="store_true",
        help="print the tour plan only; skip per-chunk narration",
    )
    args = p.parse_args()
    return asyncio.run(_amain(args.pr_url, args.mock, args.plan_only))


if __name__ == "__main__":
    raise SystemExit(main())
