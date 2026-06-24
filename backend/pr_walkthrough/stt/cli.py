"""CLI entry point for the STT adapter.

Usage
-----
Transcribe a file::

    python -m pr_walkthrough.stt.cli path/to/audio.wav

Read from stdin (pipe)::

    cat audio.webm | python -m pr_walkthrough.stt.cli -

Output format (one line to stdout)::

    "<transcribed text>"\\t<confidence>

Example::

    "hello world"\\t0.92

MIME detection
--------------
The extension of the filename is used to pick the MIME type.
When reading from stdin (``-``), pass ``--mime audio/webm`` (default) or the
appropriate type for your input.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
import sys
from pathlib import Path


def _ext_to_mime(path: str) -> str:
    """Guess MIME type from file extension; fall back to audio/wav."""
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("audio/"):
        return mime
    ext = Path(path).suffix.lower()
    fallbacks = {
        ".webm": "audio/webm",
        ".wav": "audio/wav",
        ".wave": "audio/wav",
        ".m4a": "audio/m4a",
        ".mp4": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
    }
    return fallbacks.get(ext, "audio/wav")


async def _run(audio_bytes: bytes, mime: str) -> None:
    from pr_walkthrough.stt.adapter import WhisperSTTAdapter

    adapter = WhisperSTTAdapter()
    text, confidence = await adapter.transcribe(audio_bytes, mime)
    # Output: "<text>"\t<confidence>
    print(f'"{text}"\t{confidence:.4f}')


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pr_walkthrough.stt.cli",
        description="Transcribe an audio file locally using faster-whisper.",
    )
    parser.add_argument(
        "audio",
        help="Path to audio file (WAV / WebM / M4A), or '-' to read from stdin.",
    )
    parser.add_argument(
        "--mime",
        default=None,
        help=(
            "MIME type override (e.g. audio/webm). "
            "Auto-detected from extension when not set."
        ),
    )
    args = parser.parse_args()

    if args.audio == "-":
        audio_bytes = sys.stdin.buffer.read()
        mime = args.mime or "audio/webm"
    else:
        p = Path(args.audio)
        if not p.exists():
            parser.error(f"File not found: {p}")
        audio_bytes = p.read_bytes()
        mime = args.mime or _ext_to_mime(str(p))

    asyncio.run(_run(audio_bytes, mime))


if __name__ == "__main__":
    main()
