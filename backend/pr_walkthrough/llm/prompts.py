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

    # File:line ranges available for anchoring (the chunk's hunks, new side).
    anchorable = []
    for h in chunk.hunks:
        start = h.new_range[0]
        end = h.new_range[0] + max(h.new_range[1] - 1, 0)
        anchorable.append(f"{h.file} lines {start}-{end}")
    anchor_hint = "\n".join(f"  • {a}" for a in anchorable)

    return f"""\
Narrate chunk {chunk.chunk_id}: {chunk.summary}

CHUNK DIFF
----------
{hunk_text}
{related_text}

ANCHORABLE LINE RANGES (use these as `anchor.line_range` values)
{anchor_hint}

AUDIENCE & VOICE
----------------
You're talking to a staff engineer who can read the code themselves. You're \
walking them through it the way you would on a screen-share: oriented, brisk, \
in flow. They'll ask follow-ups if they want depth. Don't over-explain. \
Don't editorialize design choices that aren't actually questionable \
("intentional", "deliberate", "the key mechanism"). Use first-person plural \
("we initialize", "we iterate", "we write") and present tense.

Concerns are part of the walkthrough, not an appendix. When you reach the \
lines a concern is about, voice it in the SAME segment that describes those \
lines — flag it the way a reviewer would on a screen-share ("…and one thing \
that's worth flagging here: …"). Then ALSO emit the same concern in the \
`concerns` field below so the side panel can track it for posting to the PR. \
Don't add a separate "concerns rundown" segment at the end; if a concern is \
worth voicing at all, voice it next to the lines it's about.

WRITE FOR THE EAR
-----------------
The narration is read aloud by TTS. The TTS engine is not great with:

  - All-caps acronyms / constants: write the spoken form. \
"RFC 5545" → "RFC fifty-five forty-five". "UID" → "U I D" or just \
"unique ID" if the literal letters aren't meaningful. \
"VEVENT" → "a calendar event". "TRANSP:OPAQUE" → \
"the transparency to opaque". "X-WR-CALNAME" → "the calendar's display name".
  - Slashes between words: don't use them. "free/busy" → "free or busy". \
"input/output" → "input and output".
  - File paths: refer to files by name or role, not by path. \
"src/auth/session.py" → "the session module" or just "session.py". Never \
read a slash-separated path aloud.
  - Naked snake_case or camelCase identifiers: refer to them by their role \
when possible ("the publisher", "the constructor"), or pronounce them \
naturally ("_stable_uid" → "stable U I D helper").

If a literal symbol name (a public API name, a function name the reviewer \
will grep for) IS the point, keep it — but accept it will be spoken \
character-by-character or awkwardly. Spell it out only when necessary.

OUTPUT
------
segments: An ORDERED list of 3-6 narration segments. Most chunks need 3-4 \
— don't pad to fill space. A segment is 1-3 short sentences. Together the \
segments walk the change in the order it makes sense to read it (entry point \
→ what it does → noteworthy callouts).

Each segment optionally carries an `anchor` — the file + line_range it's \
talking about. When set, the UI highlights and scrolls to those lines while \
the segment plays. The segment IS the highlight; there is no separate \
highlights list.

  - line_range is [start, end] inclusive, on the new (post-change) side, \
chosen from the ANCHORABLE LINE RANGES above.
  - Keep anchors tight to what you're actually talking about (1-15 lines is \
typical; whole hunks rarely).
  - Omit `anchor` only for: a one-sentence orienting intro, a transition, or \
a genuine cross-file observation. Most segments should be anchored.

related_code: Include the provided related-code snippets if genuinely \
relevant. Don't invent snippets — only use what was provided. Set relationship \
to one of: definition, callsite, test, prior_version, sibling.

concerns: 0-3 items. Should mirror the concerns you already voiced inside \
the segments — the side-panel form is for the flag/post-to-PR workflow. \
Write `suggested_question` as ready-to-post PR comment wording. Don't add \
concerns here that weren't also mentioned in a segment; if a concern isn't \
worth saying aloud, it isn't worth tracking.

look_closer_for: 0-3 short strings calling attention to subtle issues or \
missing pieces (schema migrations, race conditions, missing tests, etc.).
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
