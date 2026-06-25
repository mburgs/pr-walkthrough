"""Prompt templates for the pr-walkthrough LLM adapter.

Each prompt has a docstring explaining its intent. The system prompt is
shared across all three call types and marked for prompt caching so the
large fixed-context block is only billed once per session. Per-call
templates are kept separate to keep the stable vs volatile split clean.
"""

from __future__ import annotations

from contracts.schemas import ChunkNarration, FollowUp, Hunk, PRMetadata, TourChunk, TourPlan
from contracts.schemas import RelatedCode


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert code reviewer acting as a knowledgeable author narrating \
a guided walkthrough of a pull request. Your job is to help a reviewer \
understand the change in an order that makes architectural sense, surfacing \
concerns worth discussing and highlighting what deserves a closer look.

Tone and style
--------------
Speak as if the PR author is sitting next to the reviewer, walking them \
through the change. Be direct and concrete. Don't pad with generic phrases \
like "This is interesting" or "Let's now look at…". Get to the substance \
immediately. Use the first person ("we're swapping…", "the key thing here \
is…"). Match the depth of explanation to the complexity of the change — \
a trivial rename deserves one sentence, a new concurrency primitive deserves \
a paragraph.

Narration quality bar
---------------------
The gold standard for tone, depth, and concern detection is the fixture \
narration in the pr_small fixture set. These examples show:
- Specific, code-grounded observations (not vague summaries)
- Named design choices and their trade-offs
- Concerns stated as reviewable questions, not assertions
- Missing-coverage observations that point to concrete gaps
- "Look closer for" items that flag subtleties the reviewer might miss

Concerns
--------
Only surface a concern if it is genuinely worth a PR comment. Low severity = \
"this is a minor inconsistency / missing assertion". High severity = \
"this could cause data loss, a security hole, or broken atomicity". Write \
the suggested_question field as ready-to-post PR comment wording — the \
reviewer should be able to copy it verbatim.

Code anchors
------------
All line numbers in highlights, related_code, and concern anchors refer to \
post-change line numbers on the new side of the diff (the 'new_range' side). \
Always provide specific, correct line numbers rather than guessing.
"""


# ---------------------------------------------------------------------------
# plan_tour helpers
# ---------------------------------------------------------------------------

def format_hunk_for_plan(index: int, hunk: Hunk) -> str:
    """Render one Hunk with a stable 0-based index for the LLM to reference."""
    return (
        f"[hunk #{index}]\n"
        f"file: {hunk.file}\n"
        f"header: {hunk.header}\n"
        f"body:\n{hunk.body}\n"
    )


def build_plan_tour_user_message(pr: PRMetadata, diff: list[Hunk]) -> str:
    """Build the user-turn message for plan_tour.

    Each hunk gets a `[hunk #N]` index. The LLM emits `hunk_ids: [N, ...]`
    per chunk; the orchestrator looks each one up and attaches the full
    Hunk object. This keeps the structured-output response small even on
    multi-thousand-line PRs (the alternative — having the LLM echo every
    diff body verbatim — blows past max_tokens and truncates).
    """
    diff_text = "\n---\n".join(format_hunk_for_plan(i, h) for i, h in enumerate(diff))

    return f"""\
Plan a guided tour for the following pull request.

PR METADATA
-----------
URL: {pr.url}
Title: {pr.title}
Author: {pr.author}
Base: {pr.base_ref}  Head: {pr.head_ref}
Description:
{pr.body or "(no description)"}

FULL DIFF ({len(diff)} hunks across {len({h.file for h in diff})} files)
-----------
{diff_text}

TASK
----
Return an ordered list of chunks that walks the reviewer through this change \
in the order that makes the most architectural sense. Lead with the core \
mechanism everything else depends on. Group related hunks into the same chunk \
when they form a single logical unit.

For each chunk, emit:
- chunk_id: "c1", "c2", … (sequential, no gaps)
- hunk_ids: the 0-based indices ([hunk #N]) of every hunk that belongs in \
  this chunk — referencing them by index only; DO NOT re-emit the diff bodies
- summary: one tight sentence shown in the chunk list UI
- rationale_for_position: one sentence explaining why this chunk appears here
- est_concern_level: "low" | "medium" | "high"

Every hunk from the diff above should appear in exactly one chunk. Index range \
is 0 to {len(diff) - 1}.
"""


# ---------------------------------------------------------------------------
# narrate_chunk helpers
# ---------------------------------------------------------------------------

def format_hunk_for_narration(hunk: Hunk) -> str:
    """Render one Hunk for the narration context block."""
    return (
        f"### {hunk.file}  {hunk.header}\n"
        f"{hunk.body}"
    )


def build_narrate_chunk_system_addendum(plan: TourPlan, diff_context: str) -> str:
    """Build the cacheable diff-context block to append to the system prompt.

    Intent: The plan summary and the full diff are stable for the entire
    session, so we place them in a second system block with cache_control so
    the token cost is amortised across all narrate_chunk calls.
    """
    chunk_list = "\n".join(
        f"  {c.chunk_id}: {c.summary} [{c.est_concern_level}]"
        for c in plan.chunks
    )
    return (
        f"SESSION CONTEXT\n"
        f"---------------\n"
        f"PR: {plan.pr.title} ({plan.pr.url})\n"
        f"Author: {plan.pr.author}\n"
        f"Tour order:\n{chunk_list}\n\n"
        f"FULL DIFF (for reference)\n"
        f"-------------------------\n"
        f"{diff_context}"
    )


def build_narrate_chunk_user_message(
    chunk: TourChunk,
    related: list[RelatedCode],
) -> str:
    """Build the user-turn message for narrate_chunk.

    Intent: Provide the specific chunk being narrated and any related-code
    snippets the context-retrieval layer has already surfaced, so Claude can
    reference them accurately in the narration and in the related_code field.
    Related code is injected here (not in the system addendum) because it
    varies per chunk.
    """
    hunk_text = "\n\n".join(format_hunk_for_narration(h) for h in chunk.hunks)

    related_text = ""
    if related:
        lines = []
        for rc in related:
            lines.append(
                f"- {rc.anchor.file} lines {rc.anchor.line_range[0]}-"
                f"{rc.anchor.line_range[1]} "
                f"[{rc.relationship}]:\n```\n{rc.snippet}\n```"
            )
        related_text = "\nRELATED CODE (already retrieved)\n" + "\n".join(lines)

    return f"""\
Narrate chunk {chunk.chunk_id}: {chunk.summary}

CHUNK DIFF
----------
{hunk_text}
{related_text}

TASK
----
Produce a ChunkNarration for chunk_id="{chunk.chunk_id}".

narration: The spoken script. 2-5 sentences of direct, concrete prose \
describing what changed, why it matters, and any design choices worth noting. \
This will be read aloud by TTS — write for the ear, not the eye. No markdown, \
no headers, no bullet points.

highlights: 0-3 line ranges in the diff that deserve visual emphasis. Include \
only if genuinely important (the central mechanism, a surprising detail). \
Omit if the whole hunk is equally important.

related_code: Include the provided related-code snippets if they are \
genuinely relevant to understanding this chunk. You may omit any that are not. \
Do not invent snippets — only use what was provided. Set relationship to one \
of: definition, callsite, test, prior_version, sibling.

concerns: 0-3 items. Only raise a concern if it is worth a PR comment. \
Write suggested_question as ready-to-post PR comment wording.

look_closer_for: 0-3 short strings calling the reviewer's attention to \
subtle issues or missing pieces (schema migrations, race conditions, missing \
tests, etc.).
"""


# ---------------------------------------------------------------------------
# answer_follow_up helpers
# ---------------------------------------------------------------------------

def build_follow_up_user_message(
    plan: TourPlan,
    history: list[ChunkNarration],
    follow_up: FollowUp,
) -> str:
    """Build the user-turn message for answer_follow_up.

    Intent: Give Claude the session history (which chunks have been narrated
    so far and their concerns) plus the current question, so answers are
    grounded in what has already been discussed. History is bounded by the
    number of chunks narrated, so it is naturally short enough for the user
    turn without caching.
    """
    history_text = ""
    if history:
        parts = []
        for narration in history:
            concern_strs = [
                f"    [{c.severity}] {c.text}"
                for c in narration.concerns
            ]
            concern_block = (
                "\n  Concerns:\n" + "\n".join(concern_strs)
                if concern_strs
                else ""
            )
            parts.append(
                f"  Chunk {narration.chunk_id}: {narration.narration[:200]}…"
                f"{concern_block}"
            )
        history_text = "NARRATED SO FAR\n---------------\n" + "\n\n".join(parts) + "\n\n"

    context_note = ""
    if follow_up.chunk_id:
        context_note = f"The reviewer is currently viewing chunk {follow_up.chunk_id}.\n"

    confidence_note = ""
    if follow_up.transcript_confidence is not None and follow_up.transcript_confidence < 0.85:
        confidence_note = (
            f"(Voice transcript confidence: {follow_up.transcript_confidence:.0%} — "
            "treat the question text as approximate.)\n"
        )

    return f"""\
{history_text}\
{context_note}\
{confidence_note}\
REVIEWER QUESTION
-----------------
{follow_up.question_text}

TASK
----
Answer the question concisely and accurately in the context of this PR. \
If the question reveals a genuine concern not yet flagged, add it to \
new_concerns. Populate references with any code anchors relevant to the \
answer (file + line range). If you are uncertain about something, say so \
rather than guessing.
"""
