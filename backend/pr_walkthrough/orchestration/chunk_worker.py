"""Background task: narrate a chunk, synth audio, persist, emit SSE events."""

from __future__ import annotations

import asyncio
import logging
import re

from contracts.schemas import CodeAnchor, TourChunk, TourPlan

from . import app_context as _ctx_module
from .event_bus import publish

log = logging.getLogger(__name__)


def tts_scrub(text: str) -> str:
    """Last-mile cleanup before handing a narration segment to TTS.

    The prompt already asks the LLM to write spoken-style prose, but a
    stray "free/busy" sometimes slips through and the local TTS reads "/"
    badly. Replace single-slash word pairs like that with " or " — but
    leave anything that looks like a path, URL, version string, or
    technical acronym (TCP/IP, I/O, L1/L2) alone.

    Rule: rewrite only when both halves are pure lowercase letters of
    length ≥ 2. That preserves the common writing-style cases
    (free/busy, read/write, client/server, input/output) while leaving
    acronyms and short tokens intact.
    """
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        # Path-like: any dot, or more than one slash → leave alone
        if "." in token or token.count("/") != 1:
            return token
        a, b = token.split("/", 1)
        if (
            len(a) >= 2 and len(b) >= 2
            and a.isalpha() and b.isalpha()
            and a.islower() and b.islower()
        ):
            return f"{a} or {b}"
        return token

    text = re.sub(r"\S+", replace, text)
    # Strip markdown backticks — the LLM sometimes wraps identifiers in them
    # for the displayed transcript; TTS would otherwise say "backtick".
    text = text.replace("`", "")
    return text


async def synth_segments_to_wav(
    tts,
    segment_texts: list[str],
) -> tuple[bytes, list[int]]:
    """Synthesise an ordered list of segment texts through any TTSAdapter
    and return (single concatenated WAV, cumulative ms offsets).

    Reused by both the chunk worker (pre-synth default engine) and the
    audio-variants endpoint (lazy synth alternate engines/filters).
    """
    from pr_walkthrough.tts._wav import (
        build_wav_bytes, pcm_from_wav, TARGET_SAMPLE_RATE,
    )

    segment_pcm: list[bytes] = []
    offsets_ms: list[int] = []
    cumulative_pcm_len = 0
    for text in segment_texts:
        seg_chunks: list[bytes] = []
        async for c in tts.synth(text):
            seg_chunks.append(c)
        seg_pcm = b"".join(pcm_from_wav(c) for c in seg_chunks)
        offsets_ms.append(cumulative_pcm_len * 1000 // (TARGET_SAMPLE_RATE * 2))
        segment_pcm.append(seg_pcm)
        cumulative_pcm_len += len(seg_pcm)
    return build_wav_bytes(b"".join(segment_pcm)), offsets_ms


async def process_chunk(
    ctx: "_ctx_module.AppContext",
    plan: TourPlan,
    chunk: TourChunk,
    session_id: str,
    level: str | None = None,
) -> None:
    """Run narration + TTS for one chunk and push SSE events.

    `level` overrides `plan.familiarity` for this run — used by the ALL
    mode flow which spawns one worker per level. When None, narrate at
    the plan's configured familiarity.
    """
    if level is not None and level != plan.familiarity:
        # The prompt builder reads plan.familiarity, so pass a per-level
        # copy down. Cheap: it's the same chunks + same PR metadata.
        plan = plan.model_copy(update={"familiarity": level})
    active_level: str = plan.familiarity
    try:
        # 1. chunk_started
        await publish(session_id, {"event_type": "chunk_started", "chunk_id": chunk.chunk_id})

        # 2. Fetch related context for this chunk (use first hunk anchor)
        related = []
        if chunk.hunks:
            h = chunk.hunks[0]
            anchor = CodeAnchor(file=h.file, line_range=(h.new_range[0], h.new_range[0]))
            try:
                related = await ctx.context.related(anchor, ctx.repo_root)
            except Exception:
                log.warning("context retrieval failed for %s", chunk.chunk_id, exc_info=True)

        # 3. Call LLM for narration
        narration = await ctx.llm.narrate_chunk(plan, chunk, related)

        # 4. Emit a narration token event (in real impl would stream; here single shot)
        await publish(
            session_id,
            {
                "event_type": "narration_token",
                "chunk_id": chunk.chunk_id,
                "text": narration.narration,
            },
        )

        # 5. Persist narration (keyed per level)
        ctx.store.save_narration(session_id, narration, level=active_level)
        ctx.store.update_current_chunk(session_id, chunk.chunk_id)

        # 6. chunk_complete
        await publish(session_id, {"event_type": "chunk_complete", "chunk_id": chunk.chunk_id})

        # 7. Synthesise audio. Per-segment synth so we know each segment's
        # start offset in the final WAV (used to drive the diff highlight as
        # audio plays).
        if narration.segments:
            audio, offsets_ms = await synth_segments_to_wav(
                ctx.tts,
                [tts_scrub(s.text) for s in narration.segments],
            )
            narration = narration.model_copy(update={"segment_offsets_ms": offsets_ms})
            ctx.store.save_narration(session_id, narration, level=active_level)
        else:
            from pr_walkthrough.tts._wav import merge_synth_chunks
            audio_chunks: list[bytes] = []
            async for chunk_bytes in ctx.tts.synth(tts_scrub(narration.narration)):
                audio_chunks.append(chunk_bytes)
            audio = merge_synth_chunks(audio_chunks)

        ctx.store.save_chunk_audio(session_id, narration.chunk_id, audio, level=active_level)
        # Also write the default variant to the audio_variants table so the
        # frontend's variant switcher can find it under the engine's name.
        default_engine = type(ctx.tts).__name__.lower().replace("ttsadapter", "")
        try:
            ctx.store.save_audio_variant(
                session_id, narration.chunk_id, default_engine, True, audio,
                narration.segment_offsets_ms or [],
            )
        except Exception:
            log.warning("failed to register default variant", exc_info=True)

        # 8. audio_ready
        audio_url = f"/sessions/{session_id}/chunks/{chunk.chunk_id}/audio"
        await publish(
            session_id,
            {
                "event_type": "audio_ready",
                "chunk_id": chunk.chunk_id,
                "url": audio_url,
            },
        )

        # 9. flag_suggested for any concerns surfaced
        for concern in narration.concerns:
            await publish(
                session_id,
                {
                    "event_type": "flag_suggested",
                    "chunk_id": chunk.chunk_id,
                    "concern": concern.model_dump(),
                },
            )

    except Exception as exc:
        log.exception("chunk worker failed for %s/%s", session_id, chunk.chunk_id)
        await publish(
            session_id,
            {
                "event_type": "error",
                "message": str(exc),
                "recoverable": True,
            },
        )
