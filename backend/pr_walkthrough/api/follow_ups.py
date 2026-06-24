"""Follow-up routes: POST /sessions/{sid}/follow-up, GET .../follow-up/{aid}/audio."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from contracts.schemas import FollowUp, FollowUpAnswer
from pr_walkthrough.orchestration import AppContext

from .deps import get_app_context

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sessions/{sid}/follow-up", response_model=FollowUpAnswer)
async def post_follow_up(
    sid: str,
    request: Request,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    """Accept JSON FollowUp or raw audio.  Returns FollowUpAnswer + audio URL header."""
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
    answer = await ctx.llm.answer_follow_up(state.plan, history, follow_up)

    answer_id = ctx.store.save_follow_up(sid, follow_up, answer)

    # Synth audio for the answer
    audio_chunks: list[bytes] = []
    async for wav_chunk in ctx.tts.synth(answer.answer_text):
        audio_chunks.append(wav_chunk)
    ctx.store.save_follow_up_audio(sid, answer_id, b"".join(audio_chunks))

    audio_url = f"/sessions/{sid}/follow-up/{answer_id}/audio"

    return Response(
        content=answer.model_dump_json(),
        media_type="application/json",
        headers={"X-Answer-Audio-Url": audio_url},
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
