# Narration style notes

The narrate prompt is tuned against the kind of walkthrough a staff engineer
would give a peer on a screen-share: oriented, brisk, in flow, no padding.

Reference narration the user wrote by hand for chunk `c1` of calsync#4
("Phase 4: availability ICS feed publisher"):

> This file exposes the ICS feed publisher class, which generates an
> RFC-compliant ICS file from a list of busy blocks and writes it to a local
> file path. The initialization takes just a file path and the main function
> is the publish method. The publish method on an earlier disk was not
> implemented — we remove that NotImplementedError and implement the method
> by initializing the calendar, adding some static values to it (product ID,
> version, calscale, availability), and then for each busy block we iterate
> through, we get a stable UID based on the block's start and end time, and
> we add the UID, datetime, timestamp, start and end timestamps to it. The
> summary is always Busy and the transparency is always opaque — those are
> static values — and then we add that to the calendar. Finally, once all
> the busy blocks are iterated through, we ensure the parent directories of
> the file exist and then we write the calendar bytes to the file and log
> our status.

Concerns are part of the walkthrough, not an appendix. The reviewer voices
them in the same breath as the code they're about — the way they would on a
screen-share. The structured `concerns` field still gets populated for the
side-panel + flag-to-PR workflow, but the segments themselves SAY each
concern aloud when reaching the relevant lines. Avoid emitting a separate
"and one more concern…" segment at the end.

Things to imitate:
- Opens with WHAT the file is and what it does, in one sentence.
- States the public surface ("init takes X, the main function is Y") before
  jumping into the body.
- Walks the implementation in flow: "initialize, then add, then for each…".
- Names the constants/values factually, doesn't editorialize ("the
  transparency is always opaque — those are static values").
- Doesn't moralize design decisions ("intentional", "the key mechanism",
  "deliberate privacy choice"). Concerns go in `concerns`, not narration.
- Closes with the final action ("write the bytes and log").
- "We" voice, present tense.

The single biggest failure mode is NARRATING THE OBVIOUS — restating what
the diff already shows. "The constructor takes a file path and wraps it in
a Path object" is patently visible. Narration adds value only when it
contains something the careful reader wouldn't get from the code alone:
intent, alternatives that were rejected, implications, hidden contracts,
surprises. If a stretch of the diff has none of those, skip it — fewer,
denser segments beats coverage.

What to avoid (recurring failure modes from earlier prompts):
- Naked constants/identifiers: "TRANSP:OPAQUE", "X-WR-CALNAME", "DTSTAMP".
  Read the role aloud ("the transparency to opaque", "the calendar's
  display name", "the timestamp") and skip the literal token.
- Slashes between words: "free/busy" → "free or busy". TTS reads "/" badly.
- File paths read literally: "src/auth/session.py" → "the session module"
  or just "session.py". The `_tts_scrub` postprocessor leaves true paths
  alone (anything containing a dot or 2+ slashes) — so don't rely on the
  scrubber to fix paths, just don't read them aloud.
- Padding sentences that add no information ("this is intentional",
  "exactly the semantic we want").
- More than ~4 segments unless the chunk genuinely has that many distinct
  steps. Don't pad to fill space.

## Sources that shaped the prompt

Drawn on for the audience-and-voice framing and for the WHAT TO SAY list:

- **[Reviewing Pull Requests — Chelsea Troy](https://chelseatroy.com/2019/12/18/reviewing-pull-requests/).**
  Gold-standard piece on review *posture*. Two ideas in particular: (a) "I'm
  not judging another developer's choices until I have asked all my questions"
  — frames concerns as questions, not assertions. (b) "Don't write a review
  that could be replaced by a proofread or a Google search" — the bar for
  what counts as worth saying. Both pushed us toward the "don't narrate the
  obvious" stance and the question-shaped `suggested_question`.

- **[Writing A Great Pull Request Description — HackerOne](https://www.hackerone.com/blog/writing-great-pull-request-description).**
  The What → Why → How → Testing → Visuals recipe. Our narration is biased
  toward What and a sliver of How; the prompt's WHAT TO SAY list intentionally
  surfaces Why, Implications, and Test gaps as first-class targets.

- **[The Senior Engineer's Guide to Code Reviews — DEV Community](https://dev.to/middleware/the-senior-engineers-guide-to-the-code-reviews-1p3b).**
  The "review in passes, each at a different layer" framing — architecture
  first, then mechanism, then edge cases, then style. Our narration ordering
  rule (open with shape, then mechanism, then implications) descends from
  this rather than file-order narration.

- **[The Renaissance of Code Documentation: Introducing Code Walkthrough — InfoQ](https://www.infoq.com/articles/code-walkthrough-documentation/).**
  Distinguishes walkthroughs from inline docs: connect multiple stations
  across the code, explain flow. Also surfaces the contrarian instinct that
  underpins the "looks straightforward but isn't" item in WHAT TO SAY:
  *if the author says 'this part is straightforward,' it deserves extra scrutiny*.

- **[Walkthrough Documentation — Swimm](https://swimm.io/blog/walkthrough-documentation-where-swimms-main-value-lies).**
  Frames a code tour as "getting familiarized with a codebase with the help
  of an experienced contributor who walks you through the code" — the mental
  model the AUDIENCE & VOICE section is pointing at.

- **[Code Reviews vs Pair Programming — DEV Community](https://dev.to/uday_rayala/code-reviews-vs-pair-programming-560i).**
  Useful for the "develop a mental model of what the code does and how the
  changes affect that model" framing — corroborates the importance of intent
  and implications over surface description.
