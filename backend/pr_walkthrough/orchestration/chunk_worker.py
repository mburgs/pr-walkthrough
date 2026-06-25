"""Background task: narrate a chunk, synth audio, persist, emit SSE events."""

from __future__ import annotations

import asyncio
import logging

from contracts.schemas import CodeAnchor, TourChunk, TourPlan

from . import app_context as _ctx_module
from .event_bus import publish

log = logging.getLogger(__name__)


async def process_chunk(
    ctx: "_ctx_module.AppContext",
    plan: TourPlan,
    chunk: TourChunk,
    session_id: str,
) -> None:
    """Run narration + TTS for one chunk and push SSE events."""
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

        # 5. Persist narration
        ctx.store.save_narration(session_id, narration)
        ctx.store.update_current_chunk(session_id, chunk.chunk_id)

        # 6. chunk_complete
        await publish(session_id, {"event_type": "chunk_complete", "chunk_id": chunk.chunk_id})

        # 7. Synthesise audio. If the LLM gave us guided-tour segments,
        # synth each one separately and concat: that way we know each
        # segment's start offset in the final WAV, which the player uses
        # to drive the diff highlight/scroll as audio plays.
        from pr_walkthrough.tts._wav import (
            merge_synth_chunks, pcm_from_wav, build_wav_bytes, TARGET_SAMPLE_RATE,
        )

        if narration.segments:
            segment_pcm: list[bytes] = []
            offsets_ms: list[int] = []
            cumulative_pcm_len = 0
            for seg in narration.segments:
                seg_chunks: list[bytes] = []
                async for c in ctx.tts.synth(seg.text):
                    seg_chunks.append(c)
                # extract PCM from each yielded chunk (mix of WAVs and raw PCM)
                seg_pcm = b"".join(pcm_from_wav(c) for c in seg_chunks)
                # Mark this segment's start *before* appending its PCM
                offsets_ms.append(
                    cumulative_pcm_len * 1000 // (TARGET_SAMPLE_RATE * 2)
                )
                segment_pcm.append(seg_pcm)
                cumulative_pcm_len += len(seg_pcm)
            audio = build_wav_bytes(b"".join(segment_pcm))
            # Persist the offsets back to the narration so the API can serve them
            narration = narration.model_copy(update={"segment_offsets_ms": offsets_ms})
            ctx.store.save_narration(session_id, narration)
        else:
            audio_chunks: list[bytes] = []
            async for chunk_bytes in ctx.tts.synth(narration.narration):
                audio_chunks.append(chunk_bytes)
            audio = merge_synth_chunks(audio_chunks)

        ctx.store.save_chunk_audio(session_id, narration.chunk_id, audio)

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
