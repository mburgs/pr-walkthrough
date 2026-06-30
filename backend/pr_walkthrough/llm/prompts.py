"""Prompt templates for the pr-walkthrough LLM adapter.

Each prompt has a docstring explaining its intent. The system prompt is
shared across all three call types and marked for prompt caching so the
large fixed-context block is only billed once per session. Per-call
templates are kept separate to keep the stable vs volatile split clean.
"""

from __future__ import annotations

from contracts.schemas import (
    ChunkNarration,
    Flag,
    FollowUp,
    Hunk,
    PRMetadata,
    RelatedCode,
    TourChunk,
    TourPlan,
)


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

How highlighting works (so you can write naturally)
---------------------------------------------------
The UI auto-highlights the relevant diff lines as your narration plays — a \
separate pass attaches each sentence of your `body` to specific lines. You \
do NOT pick line numbers and you should NOT spell them out in prose. Say \
what the code does ("the rotate helper deletes every session for the user"), \
not where it sits ("on line 56, rotate deletes…"). Code anchors on \
`concerns` and `related_code` still use new-side line numbers; only the \
narration text itself is hands-off.
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

    Plain diff form — `+`/`-`/` ` markers + content. Historically this
    prefixed each line with `L<new-side line number>` so the model could
    cite anchors verbatim, but the model no longer picks line numbers —
    a separate anchor pass handles that, and giving the model numbers
    here just tempts it to mention them in the prose (which the prompt
    explicitly forbids).
    """
    lines = [f"### {hunk.file}  {hunk.header}"]
    lines.extend(hunk.body.splitlines())
    return "\n".join(lines)


# Per-familiarity guidance appended to the narrate_chunk system addendum.
# Each block is additive on top of the SYSTEM_PROMPT's "WHAT TO SAY" rules —
# it tells the model where on the verbosity dial to land. The wording
# below is intentionally written like coaching instructions, not bullet
# points, so the model treats it as a posture rather than a checklist.
_FAMILIARITY_BLOCKS: dict[str, str] = {
    "tutorial": """\
NARRATION DEPTH: tutorial
--------------------------
The reviewer is new to this language/framework. On top of the standard \
tour, briefly explain language constructs that might confuse a newcomer \
when they appear (decorators, async/await semantics, generator \
expressions, type-hinting quirks, framework-specific magic). Treat \
unusual syntax as a teaching moment — one short clause is enough; \
don't lecture. Aim for 4-7 segments instead of 2-5. Longer is fine when \
it earns its keep with pedagogy; never when it just restates code.\
""",
    "tour": """\
NARRATION DEPTH: tour
---------------------
The reviewer knows the language/framework but is new to this repo. \
Surface the repo's conventions where they shape this change: what's \
idiomatic here, what patterns this PR follows or breaks, where the \
change sits architecturally. Skip language tutoring; do orient the \
reviewer to the codebase culture. Aim for 3-6 segments.\
""",
    "review": """\
NARRATION DEPTH: review
-----------------------
The reviewer knows the language AND the repo well. Skip explanations of \
framework idioms or repo conventions — assume them. Focus on this \
specific change: what it does to the system, what it changes for \
callers, what's risky. Aim for the standard 2-5 segments.\
""",
    "highlights": """\
NARRATION DEPTH: highlights
---------------------------
The reviewer is broadly familiar with the change already and wants only \
the high-impact moments. Cut orienting context. Be ruthless about \
trimming anything that's description, transition, or summary. Aim for \
2-3 dense body sentences; one can be a clause if that's where the \
substance lives. Skip the `intro` field at this depth — at this density \
there's nothing to frame, only substance.\
""",
}


def build_narrate_chunk_system_addendum(plan: TourPlan, diff_context: str) -> str:
    """Build the cacheable diff-context block to append to the system prompt.

    Intent: The plan summary and the full diff are stable for the entire
    session, so we place them in a second system block with cache_control so
    the token cost is amortised across all narrate_chunk calls.

    Familiarity-level guidance is appended at the bottom so a session's
    chosen depth steers every narrate_chunk call without us re-shipping
    the diff each time.
    """
    chunk_list = "\n".join(
        f"  {c.chunk_id}: {c.summary} [{c.est_concern_level}]"
        for c in plan.chunks
    )
    familiarity_block = _FAMILIARITY_BLOCKS.get(plan.familiarity, _FAMILIARITY_BLOCKS["review"])
    return (
        f"SESSION CONTEXT\n"
        f"---------------\n"
        f"PR: {plan.pr.title} ({plan.pr.url})\n"
        f"Author: {plan.pr.author}\n"
        f"Tour order:\n{chunk_list}\n\n"
        f"FULL DIFF (for reference)\n"
        f"-------------------------\n"
        f"{diff_context}\n\n"
        f"{familiarity_block}"
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

CONCERNS GO INSIDE THE BODY, NOT AT THE END
-------------------------------------------
When you reach the code a concern is about, voice it inline ("…one thing \
worth flagging here: …"). Phrase it the way a reviewer would — a question, \
not a critique. Also emit the same item in the `concerns` field below for \
the side-panel + post-to-PR workflow. No separate "concerns rundown" \
paragraph at the end; if a concern isn't worth voicing where the code is, \
it isn't worth tracking.

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
intro: OPTIONAL. One short paragraph — 1-2 sentences — of orientation that \
doesn't point at specific lines. Use it for whole-file or whole-chunk framing \
the reviewer needs BEFORE diving in: "this file is the new entry point for \
the auth flow"; "the whole class is being rewritten as a dataclass"; "this \
is dead code being removed wholesale". Plays first, with no diff highlight, \
so the framing lands before lines start lighting up. Set to null when there's \
no framing of that nature to add. Don't reach for an intro — most chunks \
don't need one. NEVER use intro as a "here's what I'm about to say" preamble \
to the body; if it could be the first sentence of body instead, it should be.

body: REQUIRED. The walkthrough prose itself. Aim for 2-5 substantive points \
of WHAT TO SAY content, written as flowing paragraphs (3-8 sentences total). \
A downstream pass automatically attaches each sentence to the lines it's \
about and drives the diff highlight, so:

  - DON'T spell out line numbers or file paths in body prose. Say what the \
code does, not where it lives.
  - DO let the topic move naturally between hunks; the highlight will follow \
your sentences.
  - DO keep sentences focused — one concrete point each. The auto-anchor \
pass works best when each sentence is clearly about one piece of code.

related_code: Include the provided related-code snippets if genuinely \
relevant. Don't invent snippets — only use what was provided. Set \
relationship to one of: definition, callsite, test, prior_version, sibling.

concerns: 0-3 items. Mirror of the concerns you voiced inside body; \
`suggested_question` is the ready-to-post PR comment wording. Don't add \
concerns here that weren't also mentioned in body.
"""


# ---------------------------------------------------------------------------
# answer_follow_up helpers
# ---------------------------------------------------------------------------

def build_follow_up_system_addendum(plan: TourPlan, diff_context: str) -> str:
    """Cacheable system block for the follow-up Q&A loop.

    Mirrors `build_narrate_chunk_system_addendum` in spirit: PR metadata,
    the full diff, and a CHUNK MAP so the model can ground answers in
    "what does this PR look like as a whole". Lives in a cached system
    block because it's stable across every follow-up in the session.
    """
    chunk_map = "\n".join(
        f"  {c.chunk_id}: {c.summary} "
        f"[{c.est_concern_level}] files={', '.join(c.files) or '(none)'}"
        for c in plan.chunks
    )
    return (
        "PR METADATA\n"
        "-----------\n"
        f"URL: {plan.pr.url}\n"
        f"Title: {plan.pr.title}\n"
        f"Author: {plan.pr.author}\n"
        f"Base: {plan.pr.base_ref}  Head: {plan.pr.head_ref}\n"
        f"Description:\n{plan.pr.body or '(no description)'}\n\n"
        "FULL DIFF\n"
        "---------\n"
        f"{diff_context}\n\n"
        "CHUNK MAP\n"
        "---------\n"
        f"{chunk_map}\n"
    )


def build_follow_up_user_message(
    plan: TourPlan,
    narrated_chunks: list[ChunkNarration],
    current_chunk: TourChunk | None,
    related_for_current: list[RelatedCode],
    flags: list[Flag],
    follow_up: FollowUp,
) -> str:
    """Build the user-turn message for answer_follow_up.

    Intent: surface everything that's local to *this* question — what's
    been narrated, what the reviewer is staring at right now, related
    code we already pulled, the running concern list — without dumping
    it all into the cached system block (which is for PR-wide stable
    context). History narration text is included in full; the prior
    truncation to 200 chars threw away the very content the model needs
    to give a continuous-feeling answer.
    """
    parts: list[str] = []

    if narrated_chunks:
        narrated_blocks = []
        for n in narrated_chunks:
            concern_lines = [
                f"    [{c.severity}] {c.text}" for c in n.concerns
            ]
            anchor_lines = []
            for seg in n.segments:
                if seg.anchor is not None:
                    a = seg.anchor
                    anchor_lines.append(
                        f"    {a.file}:{a.line_range[0]}-{a.line_range[1]}"
                    )
            extras = []
            if concern_lines:
                extras.append("  Concerns:\n" + "\n".join(concern_lines))
            if anchor_lines:
                extras.append("  Anchors:\n" + "\n".join(anchor_lines))
            extra_text = ("\n" + "\n".join(extras)) if extras else ""
            narrated_blocks.append(
                f"Chunk {n.chunk_id}:\n{n.narration}{extra_text}"
            )
        parts.append(
            "NARRATED SO FAR\n---------------\n" + "\n\n".join(narrated_blocks)
        )

    if current_chunk is not None:
        hunk_text = "\n\n".join(
            format_hunk_for_narration(h) for h in current_chunk.hunks
        )
        parts.append(
            f"CURRENT CHUNK\n-------------\n"
            f"{current_chunk.chunk_id}: {current_chunk.summary}\n\n"
            f"{hunk_text}"
        )

    if related_for_current:
        lines = []
        for rc in related_for_current:
            a = rc.anchor
            lines.append(
                f"- {a.file}:{a.line_range[0]}-{a.line_range[1]} "
                f"[{rc.relationship}]\n```\n{rc.snippet}\n```"
            )
        parts.append("RELATED CODE\n------------\n" + "\n".join(lines))

    if flags:
        lines = []
        for f in flags:
            anchor_str = (
                f" @ {f.anchor.file}:{f.anchor.line_range[0]}-{f.anchor.line_range[1]}"
                if f.anchor is not None
                else ""
            )
            lines.append(f"- [{f.severity}]{anchor_str}\n  {f.body}")
        parts.append(
            "EXISTING FLAGS (don't propose duplicates as new_concerns)\n"
            "---------------------------------------------------------\n"
            + "\n".join(lines)
        )

    context_note = ""
    if follow_up.chunk_id:
        context_note = (
            f"The reviewer is currently viewing chunk {follow_up.chunk_id}."
        )

    confidence_note = ""
    if (
        follow_up.transcript_confidence is not None
        and follow_up.transcript_confidence < 0.85
    ):
        confidence_note = (
            f"(Voice transcript confidence: "
            f"{follow_up.transcript_confidence:.0%} — treat the question "
            "text as approximate.)"
        )

    notes = "\n".join(s for s in (context_note, confidence_note) if s)
    if notes:
        parts.append(notes)

    parts.append(
        "REVIEWER QUESTION\n-----------------\n" + follow_up.question_text
    )

    parts.append(
        "TASK\n----\n"
        "Answer the question concisely and accurately in the context of "
        "this PR. You have two retrieval tools available — `read_file_lines` "
        "and `grep_repo` — for looking up code outside the diff (helper "
        "definitions, callers, related types). Use them when the answer "
        "depends on code you can't see in the diff or the related-code "
        "block; skip them when the diff already shows everything you need. "
        "Once you have what you need, call `emit_follow_up_answer` exactly "
        "once. If the question reveals a genuine concern not already in "
        "EXISTING FLAGS, add it to new_concerns. Populate references with "
        "any code anchors relevant to the answer. If you are uncertain "
        "about something, say so rather than guessing."
    )

    return "\n\n".join(parts) + "\n"
