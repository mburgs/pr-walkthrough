"""Background task: narrate a chunk, synth audio, persist, emit SSE events."""

from __future__ import annotations

import asyncio
import logging
import re

from contracts.schemas import (
    ChunkNarration,
    CodeAnchor,
    NarrationSegment,
    TourChunk,
    TourPlan,
)

from . import app_context as _ctx_module
from .event_bus import publish

log = logging.getLogger(__name__)


async def _publish_phase(session_id: str, chunk_id: str, phase: str) -> None:
    """Emit a phase_changed SSE event for one chunk.

    Phases progress narrating → anchoring + synthesizing (parallel) →
    ready. The UI shows whichever phase started most recently, which
    flattens the parallel pair into a sequential reading order for
    the user (anchoring fires first because it's typically faster,
    then synthesizing overwrites it as TTS begins).
    """
    await publish(session_id, {
        "event_type": "phase_changed",
        "chunk_id": chunk_id,
        "phase": phase,
    })


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

        # 1a. Persistent cache short-circuit. Keyed by (repo, head_sha,
        # chunk_id, level, prompt_version) so a repeat run on the same
        # SHA skips the LLM+TTS round-trip entirely. Cache writes
        # happen after a successful run further down.
        cache = getattr(ctx, "cache", None)
        narr_key = audio_key = None
        if cache is not None:
            from pr_walkthrough.cache import narration_cache_key, audio_cache_key
            narr_key = narration_cache_key(
                plan.pr.repo, plan.pr.head_sha, chunk.chunk_id, active_level,
            )
            cached_narration = cache.get_narration(narr_key)
            if cached_narration is not None:
                audio_key = audio_cache_key(
                    cached_narration.narration,
                    type(ctx.tts).__name__.lower().replace("ttsadapter", ""),
                )
                cached_audio = cache.get_audio(audio_key)
                if cached_audio is not None:
                    audio, _offsets = cached_audio
                    log.info("cache hit: %s/%s (%s)", session_id, chunk.chunk_id, active_level)
                    await _finalise_chunk(
                        ctx, plan, chunk, cached_narration, audio,
                        session_id, active_level,
                    )
                    return

        # 2. Fetch related context for this chunk (use first hunk anchor)
        related = []
        if chunk.hunks:
            h = chunk.hunks[0]
            anchor = CodeAnchor(file=h.file, line_range=(h.new_range[0], h.new_range[0]))
            try:
                related = await ctx.context.related(anchor, ctx.repo_root_for(plan))
            except Exception:
                log.warning("context retrieval failed for %s", chunk.chunk_id, exc_info=True)

        # 3. Call LLM for narration (phase: narrating). Gated by
        # `llm_semaphore` so multi-level prefetch doesn't churn through
        # API rate limits. We use the split-phase adapter API
        # (`prep_narration` → `assign_anchors_to_sentences`) when the
        # adapter exposes it, so the anchor pass and TTS can run
        # concurrently below. The FakeLLM (tests) falls back to the
        # one-shot `narrate_chunk` and skips parallelisation.
        await _publish_phase(session_id, chunk.chunk_id, "narrating")
        use_parallel = hasattr(ctx.llm, "prep_narration") and hasattr(
            ctx.llm, "assign_anchors_to_sentences"
        )
        if use_parallel:
            async with ctx.llm_semaphore:
                draft = await ctx.llm.prep_narration(plan, chunk, related)
            narration, audio = await _parallel_anchor_and_synth(
                ctx, plan, chunk, draft, session_id,
            )
        else:
            async with ctx.llm_semaphore:
                narration = await ctx.llm.narrate_chunk(plan, chunk, related)
            await _publish_phase(session_id, chunk.chunk_id, "synthesizing")
            narration, audio = await _synth_for_narration(ctx, narration)

        # 4–9. Persist + emit downstream events. Same path for
        # freshly-computed and cache-hit narrations.
        await _finalise_chunk(
            ctx, plan, chunk, narration, audio, session_id, active_level,
        )

        # 10. Write back to the persistent cache so the next run on the
        # same head_sha skips the LLM + TTS work. Audio key derives from
        # the narration text we just produced.
        if cache is not None and narr_key is not None:
            try:
                cache.put_narration(narr_key, narration)
                audio_key = audio_cache_key(
                    narration.narration,
                    type(ctx.tts).__name__.lower().replace("ttsadapter", ""),
                )
                cache.put_audio(audio_key, audio, narration.segment_offsets_ms or [])
            except Exception:
                log.warning("persistent cache write failed", exc_info=True)

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


async def _finalise_chunk(
    ctx: "_ctx_module.AppContext",
    plan: TourPlan,
    chunk: TourChunk,
    narration: ChunkNarration,
    audio: bytes,
    session_id: str,
    active_level: str,
) -> None:
    """Persist + emit the post-narration events.

    Shared between the fresh-work path and the cache-hit path so both
    leave the session in the same state (store rows written, SSE events
    fired, concerns surfaced).
    """
    await publish(
        session_id,
        {
            "event_type": "narration_token",
            "chunk_id": chunk.chunk_id,
            "text": narration.narration,
        },
    )

    ctx.store.update_current_chunk(session_id, chunk.chunk_id)
    await publish(session_id, {"event_type": "chunk_complete", "chunk_id": chunk.chunk_id})

    ctx.store.save_narration(session_id, narration, level=active_level)
    ctx.store.save_chunk_audio(session_id, narration.chunk_id, audio, level=active_level)

    default_engine = type(ctx.tts).__name__.lower().replace("ttsadapter", "")
    try:
        ctx.store.save_audio_variant(
            session_id, narration.chunk_id, default_engine, True, audio,
            narration.segment_offsets_ms or [],
        )
    except Exception:
        log.warning("failed to register default variant", exc_info=True)

    audio_url = f"/sessions/{session_id}/chunks/{chunk.chunk_id}/audio"
    await publish(
        session_id,
        {
            "event_type": "audio_ready",
            "chunk_id": chunk.chunk_id,
            "url": audio_url,
        },
    )
    await _publish_phase(session_id, chunk.chunk_id, "ready")

    for concern in narration.concerns:
        await publish(
            session_id,
            {
                "event_type": "flag_suggested",
                "chunk_id": chunk.chunk_id,
                "concern": concern.model_dump(),
            },
        )


async def _parallel_anchor_and_synth(
    ctx: "_ctx_module.AppContext",
    plan: TourPlan,
    chunk: TourChunk,
    draft,
    session_id: str,
) -> tuple[ChunkNarration, bytes]:
    """Run anchor assignment and TTS concurrently from a NarrationDraft.

    TTS speaks intro (if any) followed by each body sentence as its own
    segment, giving us per-sentence offsets. After the anchor pass
    returns per-sentence anchors, `merge_with_offsets` groups
    consecutive same-anchor sentences and carries the first-sentence
    offset forward so the player highlights line up with audio.
    """
    from pr_walkthrough.llm.anchor_pass import merge_with_offsets

    tts_texts: list[str] = []
    if draft.intro:
        tts_texts.append(tts_scrub(draft.intro))
    tts_texts.extend(tts_scrub(s) for s in draft.body_sentences)

    async def _anchor_task() -> list[CodeAnchor | None]:
        await _publish_phase(session_id, chunk.chunk_id, "anchoring")
        async with ctx.llm_semaphore:
            return await ctx.llm.assign_anchors_to_sentences(
                draft.body_sentences, chunk,
            )

    async def _synth_task() -> tuple[bytes, list[int]]:
        await _publish_phase(session_id, chunk.chunk_id, "synthesizing")
        async with ctx.tts_semaphore:
            return await synth_segments_to_wav(ctx.tts, tts_texts)

    sentence_anchors, (audio, sentence_offsets_ms) = await asyncio.gather(
        _anchor_task(), _synth_task(),
    )

    # Split intro offset off the front (it doesn't go through the anchor
    # pass — intro is unanchored by design). Remaining offsets are 1:1
    # with body sentences.
    if draft.intro:
        intro_offset = sentence_offsets_ms[0]
        body_offsets = sentence_offsets_ms[1:]
    else:
        intro_offset = None
        body_offsets = sentence_offsets_ms

    body_segments, body_seg_offsets = merge_with_offsets(
        draft.body_sentences, sentence_anchors, body_offsets,
    )

    segments: list[NarrationSegment] = []
    seg_offsets: list[int] = []
    if draft.intro:
        segments.append(NarrationSegment(text=draft.intro.strip(), anchor=None))
        seg_offsets.append(intro_offset or 0)
    segments.extend(body_segments)
    seg_offsets.extend(body_seg_offsets)

    narration = ChunkNarration(
        chunk_id=draft.chunk_id,
        narration=" ".join(s.text for s in segments),
        intro=draft.intro,
        segments=segments,
        segment_offsets_ms=seg_offsets,
        related_code=draft.related_code,
        concerns=draft.concerns,
    )
    return narration, audio


async def _synth_for_narration(
    ctx: "_ctx_module.AppContext",
    narration: ChunkNarration,
) -> tuple[ChunkNarration, bytes]:
    """Sequential-path TTS for the FakeLLM/legacy `narrate_chunk` flow.

    Used only when the LLM adapter doesn't expose the split-phase API
    (`prep_narration` / `assign_anchors_to_sentences`). Returns the
    narration with segment_offsets_ms filled in, plus the audio bytes.
    """
    async with ctx.tts_semaphore:
        if narration.segments:
            audio, offsets_ms = await synth_segments_to_wav(
                ctx.tts,
                [tts_scrub(s.text) for s in narration.segments],
            )
            updated = narration.model_copy(update={"segment_offsets_ms": offsets_ms})
            return updated, audio
        from pr_walkthrough.tts._wav import merge_synth_chunks
        audio_chunks: list[bytes] = []
        async for chunk_bytes in ctx.tts.synth(tts_scrub(narration.narration)):
            audio_chunks.append(chunk_bytes)
        return narration, merge_synth_chunks(audio_chunks)
