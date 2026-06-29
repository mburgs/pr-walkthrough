"""Phonetic substitutions applied before TTS synth.

Kokoro (and other neural TTS) routinely fumble common coding jargon —
"json" becomes "jay-sohn-uh", "regex" becomes "ree-gex" with a hard g,
acronyms like LLM/MLX/SDK get spelled-out poorly. The substitutions
below rewrite the offending tokens to spellings the phonemizer
handles cleanly.

Applied with whole-word boundaries (regex `\\b`) so we don't mangle
identifiers inside narration prose. We don't try to fix typo-level
mistakes — only well-known jargon the model can't reliably pronounce.
"""

from __future__ import annotations

import re

# Tuples are (pattern_source, replacement). We compile with `re.IGNORECASE`,
# so the pattern itself is lowercase. The replacement is the exact spelling
# we want the phonemizer to see — case doesn't propagate, the TTS engine
# treats it phonetically either way.
_SUBSTITUTIONS: list[tuple[str, str]] = [
    # File formats / data
    (r"\bjson\b", "jason"),
    (r"\byaml\b", "yamel"),
    (r"\btoml\b", "tom-ell"),
    (r"\bsqlite\b", "sequel-lite"),
    (r"\bregex\b", "reg-ex"),
    (r"\bregexes\b", "reg-exes"),
    # Acronyms Kokoro mispronounces as words
    (r"\bmlx\b", "M L X"),
    (r"\bllm\b", "L L M"),
    (r"\bllms\b", "L L Ms"),
    (r"\bstt\b", "S T T"),
    (r"\btts\b", "T T S"),
    (r"\bsdk\b", "S D K"),
    (r"\bapi\b", "A P I"),
    (r"\bapis\b", "A P Is"),
    (r"\burl\b", "U R L"),
    (r"\burls\b", "U R Ls"),
    (r"\bcli\b", "C L I"),
    (r"\bsse\b", "S S E"),
    (r"\bcors\b", "korz"),
    (r"\bsql\b", "sequel"),
    (r"\boauth\b", "oh-auth"),
    (r"\bnginx\b", "engine X"),
    (r"\bpostgres\b", "post-gres"),
    (r"\bredis\b", "red-iss"),
    # Code-ish bits that show up in narration
    (r"\bauth\b", "awth"),
    (r"\bargs\b", "args"),  # keep but normalize variants
    (r"\bkwargs\b", "kay-w-args"),
    (r"\bdef\b", "def"),  # no-op, listed for documentation
]

_COMPILED = [(re.compile(pat, re.IGNORECASE), repl) for pat, repl in _SUBSTITUTIONS]


def normalize_for_tts(text: str) -> str:
    """Apply phonetic substitutions to `text` before sending to TTS.

    Operates on a best-effort basis: unknown tokens pass through
    unchanged. Substitutions are word-bounded so embedded matches
    (e.g. "json" inside "jsonschema") are NOT replaced.
    """
    for rx, repl in _COMPILED:
        text = rx.sub(repl, text)
    return text
