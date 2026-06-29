#!/usr/bin/env python3
"""Evaluation harness for the second-pass anchor assignment.

Runs `reassign_anchors` against fixture narrations (and optionally a
live PR's narrations) across a set of models, printing side-by-side
output so a human can compare segment grouping, anchor accuracy, and
cost across model tiers.

Usage:
    .venv/bin/python scripts/eval_anchor_pass.py                # fixtures only
    .venv/bin/python scripts/eval_anchor_pass.py --pr <url>     # also live PR
    .venv/bin/python scripts/eval_anchor_pass.py --models haiku,sonnet

Requires ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

import anthropic  # noqa: E402

from contracts.schemas import ChunkNarration, TourChunk, TourPlan  # noqa: E402
from pr_walkthrough.llm.anchor_pass import reassign_anchors  # noqa: E402


MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}

# Approximate per-million token pricing (USD) — for ballpark cost only.
# Update if Anthropic pricing changes; this is just for the report.
PRICING_PER_MTOK = {
    "claude-haiku-4-5":  {"in": 1.00, "out": 5.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "claude-opus-4-7":   {"in": 15.00, "out": 75.00},
}


def cost_usd(model: str, in_tok: int, out_tok: int) -> float:
    p = PRICING_PER_MTOK.get(model)
    if not p:
        return 0.0
    return (in_tok / 1_000_000) * p["in"] + (out_tok / 1_000_000) * p["out"]


def _format_segment(idx: int, seg) -> str:
    if seg.anchor is None:
        anchor_str = "(no anchor)"
    else:
        f = seg.anchor.file
        a, b = seg.anchor.line_range
        anchor_str = f"{f}:{a}-{b}" if a != b else f"{f}:{a}"
    return f"  [{idx}] @{anchor_str}\n      {seg.text}"


def print_section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def print_subsection(title: str) -> None:
    print()
    print("-" * 78)
    print(title)
    print("-" * 78)


async def evaluate_one(
    label: str,
    narration: ChunkNarration,
    chunk: TourChunk,
    client: anthropic.AsyncAnthropic,
    models: list[str],
) -> None:
    print_section(f"CHUNK: {label}")
    print(f"hunks: {len(chunk.hunks)} | original segments: {len(narration.segments)}")
    print(f"narration: {len(narration.narration)} chars")

    print_subsection("ORIGINAL (from first pass)")
    for i, s in enumerate(narration.segments):
        print(_format_segment(i, s))

    for model in models:
        try:
            rewritten, metrics = await reassign_anchors(narration, chunk, client, model)
        except Exception as e:
            print_subsection(f"REASSIGNED via {model} — FAILED")
            print(f"  {type(e).__name__}: {e}")
            continue
        usd = cost_usd(model, metrics.input_tokens, metrics.output_tokens)
        print_subsection(
            f"REASSIGNED via {model}  "
            f"[{metrics.sentence_count} sentences → {metrics.segment_count} segments | "
            f"{metrics.latency_ms} ms | "
            f"{metrics.input_tokens} in / {metrics.output_tokens} out tok | "
            f"${usd:.4f}]"
        )
        for i, s in enumerate(rewritten.segments):
            print(_format_segment(i, s))


def _load_fixture(name: str) -> tuple[str, ChunkNarration, TourChunk]:
    fix_dir = REPO_ROOT / "fixtures" / "pr_small"
    plan = TourPlan.model_validate(json.loads((fix_dir / "tour_plan.json").read_text()))
    narration = ChunkNarration.model_validate(
        json.loads((fix_dir / "chunks" / f"{name}.narration.json").read_text())
    )
    chunk = next(c for c in plan.chunks if c.chunk_id == name)
    return f"fixture {name}", narration, chunk


async def _generate_live_narrations(
    pr_url: str, models_for_first_pass: str,
) -> list[tuple[str, ChunkNarration, TourChunk]]:
    """Pull a fresh tour plan + narrations from a real PR.

    The first-pass narration uses sonnet (the production default) since
    we want this evaluation to reflect what the live system produces.
    Only the second pass varies model.
    """
    from pr_walkthrough.pr.gh_source import GhPRSource
    from pr_walkthrough.llm.adapter import ClaudeLLMAdapter

    print(f"\n[live] fetching PR {pr_url} …")
    src = GhPRSource()
    pr, hunks = await src.fetch(pr_url)
    llm = ClaudeLLMAdapter()
    print(f"[live] planning tour ({len(hunks)} hunks) …")
    plan = await llm.plan_tour(pr, hunks)
    out: list[tuple[str, ChunkNarration, TourChunk]] = []
    # Cap at 2 chunks to keep cost / runtime reasonable for the eval.
    for chunk in plan.chunks[:2]:
        print(f"[live] narrating {chunk.chunk_id} (sonnet, first pass) …")
        narration = await llm.narrate_chunk(plan, chunk, related=[])
        out.append((f"live {pr_url}#{chunk.chunk_id}", narration, chunk))
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        default="haiku,sonnet,opus",
        help="comma-separated model aliases (haiku, sonnet, opus) or full IDs",
    )
    parser.add_argument(
        "--pr",
        default=None,
        help="optional PR URL; if set, runs against a live narration from this PR",
    )
    parser.add_argument(
        "--fixtures",
        default="c1,c2,c3",
        help="comma-separated fixture chunk_ids (default: c1,c2,c3)",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(2)

    models = [
        MODEL_ALIASES.get(m.strip(), m.strip())
        for m in args.models.split(",")
        if m.strip()
    ]
    print(f"models under test: {models}")

    targets: list[tuple[str, ChunkNarration, TourChunk]] = []
    for name in args.fixtures.split(","):
        name = name.strip()
        if name:
            targets.append(_load_fixture(name))

    if args.pr:
        targets.extend(await _generate_live_narrations(args.pr, models[0]))

    client = anthropic.AsyncAnthropic()
    for label, narration, chunk in targets:
        await evaluate_one(label, narration, chunk, client, models)

    print()
    print("done.")


if __name__ == "__main__":
    asyncio.run(main())
