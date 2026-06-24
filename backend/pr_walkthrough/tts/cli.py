"""CLI entry point for pr_walkthrough.tts.

Usage:
    python -m pr_walkthrough.tts.cli "<text>" --out out.wav [--voice <name>] [--engine kokoro|piper|say]

Produces a 22 050 Hz, 16-bit, mono WAV file ready for playback:
    afplay out.wav                    # macOS
    aplay out.wav                     # Linux

Engine selection:
    --engine kokoro   use Kokoro 82M (default if installed)
    --engine piper    use Piper (requires voice model, see README)
    --engine say      use macOS say + afconvert (macOS only)
    (omit --engine)   auto-select best available engine
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path


async def _run(text: str, out: Path, engine: str | None, voice: str) -> None:
    from pr_walkthrough.tts import make_tts

    adapter = make_tts(engine=engine)

    print(f"Engine : {adapter.__class__.__name__}", file=sys.stderr)
    print(f"Voice  : {voice}", file=sys.stderr)
    print(f"Text   : {text[:80]}{'…' if len(text) > 80 else ''}", file=sys.stderr)

    t0 = time.perf_counter()
    wav_chunks: list[bytes] = []

    stream = await adapter.synth(text, voice=voice)
    async for chunk in stream:
        wav_chunks.append(chunk)

    elapsed = time.perf_counter() - t0
    words = len(text.split())
    print(f"Synth  : {elapsed:.2f}s for {words} words", file=sys.stderr)

    # Reassemble: first chunk is a complete WAV; append remaining PCM.
    # To produce a single playable file we need to rebuild the WAV properly.
    # The adapters each yield chunk 0 as a full WAV; subsequent chunks are
    # raw PCM.  Re-wrap all PCM into one WAV.
    from pr_walkthrough.tts._wav import build_wav_bytes

    import wave
    import io

    if not wav_chunks:
        print("ERROR: synth produced no output", file=sys.stderr)
        sys.exit(1)

    # Extract PCM from first chunk (which is a full WAV)
    first_wav = wav_chunks[0]
    buf = io.BytesIO(first_wav)
    with wave.open(buf, "rb") as wf:
        first_pcm = wf.readframes(wf.getnframes())

    # Remaining chunks are raw PCM
    rest_pcm = b"".join(wav_chunks[1:])
    all_pcm = first_pcm + rest_pcm

    final_wav = build_wav_bytes(all_pcm)
    out.write_bytes(final_wav)

    size_kb = len(final_wav) / 1024
    print(f"Output : {out} ({size_kb:.1f} KB)", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pr_walkthrough.tts.cli",
        description="Local TTS: text → WAV (22 050 Hz, 16-bit, mono)",
    )
    parser.add_argument("text", help="Text to synthesize")
    parser.add_argument("--out", required=True, type=Path, help="Output WAV file path")
    parser.add_argument(
        "--engine",
        choices=["kokoro", "piper", "say"],
        default=None,
        help="Force a specific TTS engine (default: auto-select best available)",
    )
    parser.add_argument(
        "--voice",
        default="default",
        help="Voice name (engine-specific). Use 'default' to let the engine choose.",
    )
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="Print available voices for the selected engine and exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    if args.list_voices:
        from pr_walkthrough.tts import make_tts

        adapter = make_tts(engine=args.engine)
        for v in adapter.available_voices():
            print(v)
        return

    asyncio.run(_run(args.text, args.out, args.engine, args.voice))


if __name__ == "__main__":
    main()
