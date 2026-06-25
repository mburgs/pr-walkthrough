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
