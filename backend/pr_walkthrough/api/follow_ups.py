"""Follow-up routes: POST /sessions/{sid}/follow-up, GET .../follow-up/{aid}/audio.

The POST endpoint returns Server-Sent Events so the frontend can render
the answer as it's generated. Event sequence:

  event: open          data: {}                       connection alive
  event: transcribing  data: {}                       voice path only —
                                                      STT in progress
  event: question      data: {"text": "...",          voice path only —
                              "confidence": 0.92}     transcribed input
  event: token         data: {"text": "...delta..."}  one per partial-JSON
                                                      fragment of answer_text
  event: final         data: {"answer": {...},        fires as soon as the
                              "audio_url": "...",     LLM is done — audio
                              "answer_id": "..."}     synth runs after this
                                                      in a background task
  event: error         data: {"message": "..."}       terminal failure

`final` is emitted *before* TTS finishes so the UI stops claiming
"streaming" the moment text is done. The audio GET endpoint long-polls
until synth completes, so the player just blocks briefly when the user
clicks play before the audio is ready.

The transport is SSE (not WebSocket) — the protocol is one-way and SSE
keeps middleware (proxies, CORS) simpler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from contracts.schemas import CodeAnchor, FollowUp
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

    # Read the full body up-front (must be done before the streaming
    # response begins). For audio inputs we defer transcription itself
    # into the SSE generator so the client can see a "transcribing"
    # status + the recognised text before tokens start flowing.
    follow_up: FollowUp | None = None
    audio_bytes: bytes | None = None
    if content_type.startswith("application/json"):
        body = await request.json()
        follow_up = FollowUp.model_validate(body)
    else:
        audio_bytes = await request.body()

    narrated = ctx.store.list_narrated_chunks(sid)
    qa = ctx.store.list_follow_up_qa(sid)
    flags = ctx.store.list_flags(sid)

    # Locate the chunk the reviewer is currently on (may be None at the
    # very start of a session or after the tour wraps) and pre-fetch
    # related code for its first hunk. Done up-front so the LLM call
    # below is purely network-bound and stays inside the streaming gen.
    current_chunk = next(
        (c for c in state.plan.chunks if c.chunk_id == state.current_chunk_id),
        None,
    )
    related_for_current: list = []
    if current_chunk and current_chunk.hunks:
        h = current_chunk.hunks[0]
        anchor = CodeAnchor(file=h.file, line_range=(h.new_range[0], h.new_range[0]))
        try:
            related_for_current = await ctx.context.related(anchor, ctx.repo_root)
        except Exception:
            log.warning("related-code lookup failed for follow-up", exc_info=True)

    # Same full-diff shape the chunk worker uses for the narration system
    # block, so the model sees the whole PR — not just the slice that's
    # been narrated so far.
    diff_context = "\n\n".join(
        f"{h.file} {h.header}\n{h.body}"
        for c in state.plan.chunks
        for h in c.hunks
    )

    async def synth_audio_bg(answer_id: str, text: str) -> None:
        """Background TTS — runs after the SSE response returns so the
        UI can flip the "streaming" indicator off as soon as text is
        done. The audio GET endpoint long-polls until this finishes."""
        from pr_walkthrough.tts._wav import merge_synth_chunks
        async with ctx.tts_semaphore:
            try:
                audio_chunks: list[bytes] = []
                async for wav_chunk in ctx.tts.synth(text):
                    audio_chunks.append(wav_chunk)
                ctx.store.save_follow_up_audio(
                    sid, answer_id, merge_synth_chunks(audio_chunks),
                )
            except Exception:
                log.exception("follow-up audio synth failed (audio will 504)")

    async def event_stream():
        nonlocal follow_up

        # Heartbeat / opener — tells the client we're alive and avoids
        # buffered proxies holding the connection silent.
        yield "event: open\ndata: {}\n\n"

        # Voice path: STT before LLM. Surface progress + the recognised
        # text so the user can see what was heard (vital when STT
        # mistakes the question — they'd otherwise see a non-sequitur
        # answer with no idea why).
        if follow_up is None:
            assert audio_bytes is not None
            yield "event: transcribing\ndata: {}\n\n"
            try:
                text, confidence = await ctx.stt.transcribe(audio_bytes, content_type)
            except Exception as e:
                log.exception("STT failed")
                yield f"event: error\ndata: {json.dumps({'message': f'Transcription failed: {e}'})}\n\n"
                return
            yield (
                "event: question\n"
                f"data: {json.dumps({'text': text, 'confidence': confidence})}\n\n"
            )
            follow_up = FollowUp(
                chunk_id=state.current_chunk_id,
                question_text=text,
                transcript_confidence=confidence,
            )

        # Stream the LLM tokens. The semaphore gate matches process_chunk
        # so a busy multi-level chunk burst doesn't elbow this call out.
        async with ctx.llm_semaphore:
            try:
                stream = await ctx.llm.answer_follow_up_streaming(
                    plan=state.plan,
                    narrated_chunks=narrated,
                    qa_history=qa,
                    current_chunk=current_chunk,
                    related_for_current=related_for_current,
                    flags=flags,
                    diff_context=diff_context,
                    repo_root=ctx.repo_root,
                    follow_up=follow_up,
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
        audio_url = f"/sessions/{sid}/follow-up/{answer_id}/audio"

        # Hand audio synth off to a background task and emit `final` now.
        # Previously we awaited synth here, which kept the "streaming"
        # indicator on for the ~5-15s synth window even though the text
        # was done. The audio GET is long-polling, so the player just
        # blocks briefly when the user clicks play before bytes exist.
        asyncio.create_task(synth_audio_bg(answer_id, answer.answer_text))

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
    """Long-poll the audio for a follow-up answer.

    The POST endpoint hands TTS synth off to a background task and
    returns `final` immediately, so this GET may arrive before the
    audio bytes exist. Poll until they do (or the timeout fires).
    """
    elapsed = 0.0
    timeout = float(os.environ.get("PR_WALKTHROUGH_AUDIO_TIMEOUT", "300"))
    poll = 0.5
    while elapsed < timeout:
        audio = ctx.store.get_follow_up_audio(sid, aid)
        if audio is not None:
            return StreamingResponse(
                _iter_bytes(audio),
                media_type="audio/wav",
                headers={"Transfer-Encoding": "chunked"},
            )
        await asyncio.sleep(poll)
        elapsed += poll
    raise HTTPException(
        status_code=504,
        detail=f"Audio for answer {aid!r} not ready within timeout",
    )


async def _iter_bytes(data: bytes, chunk_size: int = 65536):
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]
