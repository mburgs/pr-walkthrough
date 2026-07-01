"""`pr-walkthrough-stt` CLI: MIME detection + the transcribe flow.

`_ext_to_mime` calls `mimetypes.guess_type`, which is backed by the host's
mime.types database and can vary across platforms/CI images. Tests patch
`mimetypes.guess_type` directly so behavior is pinned to this module's own
branching logic (audio guess passthrough -> fallback table -> hard default)
rather than to whatever happens to be installed on the runner.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pr_walkthrough.stt import cli as stt_cli
from pr_walkthrough.stt.cli import _ext_to_mime, _run, main


def test_ext_to_mime_uses_system_guess_when_it_is_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt_cli.mimetypes, "guess_type", lambda p: ("audio/x-wav", None))
    assert _ext_to_mime("clip.wav") == "audio/x-wav"


def test_ext_to_mime_falls_back_to_table_when_guess_is_not_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # e.g. some systems map .webm to "video/webm" — not audio/*, so the
    # extension-keyed fallback table should be used instead.
    monkeypatch.setattr(stt_cli.mimetypes, "guess_type", lambda p: ("video/webm", None))
    assert _ext_to_mime("clip.webm") == "audio/webm"


def test_ext_to_mime_falls_back_to_table_when_guess_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stt_cli.mimetypes, "guess_type", lambda p: (None, None))
    assert _ext_to_mime("clip.m4a") == "audio/m4a"
    assert _ext_to_mime("clip.mp3") == "audio/mpeg"


def test_ext_to_mime_defaults_to_wav_for_unknown_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stt_cli.mimetypes, "guess_type", lambda p: (None, None))
    assert _ext_to_mime("clip.xyz") == "audio/wav"


async def test_run_prints_quoted_text_and_confidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from pr_walkthrough.stt.parakeet_adapter import ParakeetSTTAdapter

    monkeypatch.setattr(
        ParakeetSTTAdapter, "transcribe", AsyncMock(return_value=("hello world", 0.9234))
    )
    await _run(b"fake-audio-bytes", "audio/wav")
    assert capsys.readouterr().out == '"hello world"\t0.9234\n'


def test_main_errors_when_file_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr("sys.argv", ["pr-walkthrough-stt", "/no/such/file.wav"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert "File not found" in capsys.readouterr().err


def test_main_transcribes_a_real_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from pr_walkthrough.stt.parakeet_adapter import ParakeetSTTAdapter

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"fake-audio-bytes")
    transcribe = AsyncMock(return_value=("ship it", 0.5))
    monkeypatch.setattr(ParakeetSTTAdapter, "transcribe", transcribe)
    monkeypatch.setattr("sys.argv", ["pr-walkthrough-stt", str(audio)])

    main()

    assert capsys.readouterr().out == '"ship it"\t0.5000\n'
    call_args, _ = transcribe.call_args
    assert call_args[0] == b"fake-audio-bytes"


def test_main_reads_stdin_when_audio_arg_is_dash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from pr_walkthrough.stt.parakeet_adapter import ParakeetSTTAdapter

    transcribe = AsyncMock(return_value=("piped", 1.0))
    monkeypatch.setattr(ParakeetSTTAdapter, "transcribe", transcribe)
    monkeypatch.setattr("sys.argv", ["pr-walkthrough-stt", "-", "--mime", "audio/webm"])

    class _FakeStdin:
        class buffer:
            @staticmethod
            def read() -> bytes:
                return b"piped-bytes"

    monkeypatch.setattr(stt_cli.sys, "stdin", _FakeStdin())

    main()

    assert capsys.readouterr().out == '"piped"\t1.0000\n'
    args, _ = transcribe.call_args
    assert args == (b"piped-bytes", "audio/webm")
