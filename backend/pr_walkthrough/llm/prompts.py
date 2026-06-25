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
All line numbers in segment, related_code, and concern anchors refer to \
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
the way a knowledgeable peer would on a screen-share. Order by narrative \
altitude — entry point or core mechanism first, then callers / wiring, then \
tests, then config or housekeeping. NOT by file order.

TOUR SHAPE
----------
- Splitting a single file's hunks across multiple chunks is encouraged when \
  the parts serve different roles in the walkthrough (e.g. a file's public \
  API surface shown early; its internals shown later near the callers).
- The same hunk MAY appear in more than one chunk if it provides essential \
  context for each. Use this sparingly — only when the cross-reference \
  actually serves the reviewer.
- For larger PRs, give adjacent chunks a shared `group` label so the sidebar \
  shows a section divider. Typical groups: 'API surface', 'Mechanism', \
  'Wiring', 'Tests', 'Config'. Pick whatever fits this PR. Small PRs (< 4 \
  chunks) usually don't need groups — leave `group: null`.

For each chunk, emit:
- chunk_id: "c1", "c2", … (sequential, no gaps)
- hunk_ids: the 0-based indices ([hunk #N]) of every hunk that belongs in \
  this chunk — referencing them by index only; DO NOT re-emit the diff bodies
- summary: one tight sentence shown in the chunk list UI
- rationale_for_position: one sentence explaining why this chunk appears here
- est_concern_level: "low" | "medium" | "high"
- group: short label (2-4 words) or null

Every hunk from the diff above should appear in AT LEAST one chunk. Index \
range is 0 to {len(diff) - 1}.
"""


# ---------------------------------------------------------------------------
# narrate_chunk helpers
# ---------------------------------------------------------------------------

def format_hunk_for_narration(hunk: Hunk) -> str:
    """Render one Hunk for the narration context block.

    Prefixes each diff line with `L<new-side line number>` so the LLM picks
    line numbers it has literally seen rather than computing them by
    arithmetic — historically the source of off-by-N anchor mistakes.

    - "+" and " " (context) lines get the new-side number.
    - "-" lines get `L----` — the deleted text has no new-side line, and
      anchors only live on the new side anyway, so they're not pickable.
    """
    lines = [f"### {hunk.file}  {hunk.header}"]
    new_line = hunk.new_range[0] or 1
    for raw in hunk.body.splitlines():
        if not raw:
            lines.append(raw)
            continue
        marker = raw[0]
        if marker == "+":
            lines.append(f"L{new_line:>4}  {raw}")
            new_line += 1
        elif marker == "-":
            lines.append(f"L----  {raw}")
        else:  # space / context
            lines.append(f"L{new_line:>4}  {raw}")
            new_line += 1
    return "\n".join(lines)


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

Each line in the CHUNK DIFF above is prefixed with `L<nnn>` for added \
or unchanged lines. Use those numbers exactly when you anchor — copy them, \
don't compute them. Lines prefixed `L----` are deletions and can't be \
anchored to (anchors live on the new side).

AUDIENCE & VOICE
----------------
You're talking to a staff engineer who is staring at the same diff you are. \
They can read the code. Your job is to add what the code DOESN'T say. \
Talk to them the way you would on a screen-share: oriented, brisk, in flow. \
First-person plural ("we initialize", "we iterate"), present tense.

DON'T NARRATE THE OBVIOUS
-------------------------
The biggest failure mode is restating what's already on screen. Examples:

  BAD:  "The constructor takes a file_path string and wraps it in a Path \
object." (We can see that.)
  GOOD: (just skip — there's nothing to say) OR \
"We're moving from accepting **kwargs to a single typed file_path — the old \
shape was a placeholder; this commits to a concrete contract." (That we \
can't see from the diff alone.)

  BAD:  "For each block we create an event, set the UID, set DTSTAMP to now, \
set start and end times, and set summary to Busy." (Verbatim re-narration.)
  GOOD: "Per-event fields are mechanical except the UID — that's the only \
piece that has to be stable across runs."

For each chunk: identify the things worth saying that a careful reader \
WOULDN'T get just by looking. Those become your segments. If a hunk is \
purely mechanical wiring, fold it into a clause in a neighbouring segment \
("…then writes via write_bytes") rather than giving it its own breath. Aim \
for FEWER segments, denser content, not coverage of every line.

WHAT TO SAY (when you do say something)
---------------------------------------
A staff engineer's walkthrough adds these, in roughly this order of priority:

  1. WHY this change — what goal it serves, what was wrong with the previous \
shape, what made this the right move now.
  2. SHAPE of the change — is this a rewrite, an extension, a swap, a \
tightening of an interface, scaffolding for what's next?
  3. ALTERNATIVES considered and rejected — what would have been simpler or \
more obvious; why this instead.
  4. IMPLICATIONS — what callers now assume, what failure modes appear, \
idempotency, ordering, schema invariants, what breaks at scale.
  5. TEST COVERAGE GAPS — what the tests don't catch; what would.
  6. THINGS THAT LOOK STRAIGHTFORWARD BUT AREN'T — if a careful reader would \
skim past it, pause on it. That's where bugs hide.

Each segment should pick from this list. If a stretch of the diff yields none \
of (1)-(6), it doesn't need a segment.

Open with whichever item gives the chunk its shape — usually (1) or (2) — \
before walking into specifics. Don't narrate file order; narrate altitude, \
high to low.

CONCERNS GO INSIDE SEGMENTS, NOT AT THE END
-------------------------------------------
When you reach lines a concern is about, voice it in the same segment ("…one \
thing worth flagging here: …"). Phrase it the way a reviewer would — a \
question, not a critique. Also emit the same item in the `concerns` field \
below for the side-panel + post-to-PR workflow. No separate "concerns rundown" \
segment at the end; if a concern isn't worth voicing where the code is, it \
isn't worth tracking.

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
segments: An ORDERED list of 2-5 narration segments (see WHAT TO SAY for what \
each should be about; bias toward fewer, denser segments).

  - Each segment optionally carries an `anchor`. When set, the UI highlights \
and scrolls to those lines while the segment plays.
  - line_range is [start, end] inclusive, on the new (post-change) side, \
chosen from the ANCHORABLE LINE RANGES above. Keep anchors tight to what \
you're actually talking about (1-15 lines is typical; whole hunks rarely).
  - Omit `anchor` only for: a one-sentence orienting intro, a transition, \
or a genuine cross-file observation. Most segments should be anchored.

related_code: Include the provided related-code snippets if genuinely \
relevant. Don't invent snippets — only use what was provided. Set \
relationship to one of: definition, callsite, test, prior_version, sibling.

concerns: 0-3 items. Mirror of the concerns you voiced inside segments; \
`suggested_question` is the ready-to-post PR comment wording. Don't add \
concerns here that weren't also mentioned in a segment.

look_closer_for: 0-3 short strings — quieter signals the reviewer should \
re-check during careful reading (e.g. "schema migration not in this PR", \
"no test covers the rotated == 0 case"). Distinct from concerns: these \
aren't PR comments, they're "open this with attention" notes.
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
