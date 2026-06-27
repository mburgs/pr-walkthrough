"""Follow-up routes: POST /sessions/{sid}/follow-up, GET .../follow-up/{aid}/audio.

The POST endpoint returns Server-Sent Events so the frontend can render
the answer as it's generated. Two event types:

  event: token   data: {"text": "...delta..."}    one per partial-JSON
                                                  fragment of answer_text
  event: final   data: {"answer": {...},          fires once after audio
                       "audio_url": "..."}        synth completes

The transport is SSE (not WebSocket) — the protocol is one-way and SSE
keeps middleware (proxies, CORS) simpler.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from contracts.schemas import FollowUp
from pr_walkthrough.orchestration import AppContext

from .deps import get_app_context

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sessions/{sid}/follow-up")
async def post_follow_up(
    sid: str,
    request: Request,
    ctx: AppContext = Depends(get_app_context),
) -> StreamingResponse:
    """Stream the follow-up answer back as SSE.

    Accepts the same request bodies as before (JSON FollowUp or raw
    audio). Response shape changed from JSON to text/event-stream — the
    frontend client handles both transports for transition.
    """
    state = ctx.store.get_session_state(sid)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Session {sid!r} not found")

    content_type = request.headers.get("content-type", "application/json")

    if content_type.startswith("application/json"):
        body = await request.json()
        follow_up = FollowUp.model_validate(body)
    else:
        # Audio — transcribe via STT
        audio_bytes = await request.body()
        text, confidence = await ctx.stt.transcribe(audio_bytes, content_type)
        follow_up = FollowUp(
            chunk_id=state.current_chunk_id,
            question_text=text,
            transcript_confidence=confidence,
        )

    history = ctx.store.list_follow_up_history(sid)

    async def event_stream():
        from pr_walkthrough.tts._wav import merge_synth_chunks

        # Heartbeat / opener — tells the client we're alive and avoids
        # buffered proxies holding the connection silent.
        yield "event: open\ndata: {}\n\n"

        # Stream the LLM tokens. The semaphore gate matches process_chunk
        # so a busy multi-level chunk burst doesn't elbow this call out.
        async with ctx.llm_semaphore:
            try:
                stream = await ctx.llm.answer_follow_up_streaming(
                    state.plan, history, follow_up,
                )
            except Exception as e:
                log.exception("follow-up LLM call failed")
                yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
                return

            async for token in stream:
                yield f"event: token\ndata: {json.dumps({'text': token})}\n\n"

            try:
                answer = stream.get_result()
            except Exception as e:
                log.exception("follow-up result extraction failed")
                yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
                return

        answer_id = ctx.store.save_follow_up(sid, follow_up, answer)

        # Audio synth runs after the text is finalised so the player has
        # something to play. Gated on the TTS semaphore for the same
        # memory-budget reasons as chunk audio.
        async with ctx.tts_semaphore:
            try:
                audio_chunks: list[bytes] = []
                async for wav_chunk in ctx.tts.synth(answer.answer_text):
                    audio_chunks.append(wav_chunk)
                ctx.store.save_follow_up_audio(
                    sid, answer_id, merge_synth_chunks(audio_chunks),
                )
            except Exception:
                # Audio is a nice-to-have; surface the answer even if synth
                # blows up. The frontend will see audio_url 404 and recover.
                log.exception("follow-up audio synth failed (continuing)")

        audio_url = f"/sessions/{sid}/follow-up/{answer_id}/audio"
        final_payload = {
            "answer": answer.model_dump(),
            "audio_url": audio_url,
            "answer_id": answer_id,
        }
        yield f"event: final\ndata: {json.dumps(final_payload)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx-style proxy buffering
        },
    )


@router.get("/sessions/{sid}/follow-up/{aid}/audio")
async def get_follow_up_audio(
    sid: str,
    aid: str,
    ctx: AppContext = Depends(get_app_context),
) -> StreamingResponse:
    audio = ctx.store.get_follow_up_audio(sid, aid)
    if audio is None:
        raise HTTPException(status_code=404, detail=f"Audio for answer {aid!r} not found")
    return StreamingResponse(
        _iter_bytes(audio),
        media_type="audio/wav",
        headers={"Transfer-Encoding": "chunked"},
    )


async def _iter_bytes(data: bytes, chunk_size: int = 65536):
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]
