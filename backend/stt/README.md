# STT Service — faster-whisper adapter

Local speech-to-text for pr-walkthrough. Audio **never leaves the machine**.

## Requirements

### System

- **Python 3.11+**
- **ffmpeg** — required for WebM/Opus → WAV conversion (browser `MediaRecorder` output).

  ```bash
  # macOS
  brew install ffmpeg

  # Debian / Ubuntu
  sudo apt install ffmpeg
  ```

### Python dependencies

```bash
pip install faster-whisper pydub numpy
```

Or install the backend package (recommended):

```bash
cd backend/
pip install -e ".[dev]"
```

## Model download (first run)

On first use, faster-whisper downloads the model from Hugging Face Hub and caches it in `~/.cache/huggingface/hub`. Subsequent runs are fully offline.

| Model | Size | Notes |
|-------|------|-------|
| `tiny` | ~75 MB | Fastest; good for quick demos |
| `base` | ~140 MB | **Default** — good accuracy/speed balance on CPU |
| `small` | ~488 MB | Better accuracy, ~2× slower |

Set the model via env var:

```bash
export PR_WALKTHROUGH_WHISPER_MODEL=tiny   # or base, small, medium, large-v3
```

After the first download, `transcribe()` makes **zero network calls**. The conformance test (`test_no_network_calls`) asserts this at the socket level.

## CLI usage

```bash
# Transcribe a file
python -m pr_walkthrough.stt.cli path/to/audio.wav

# Read from stdin
cat audio.webm | python -m pr_walkthrough.stt.cli - --mime audio/webm
```

Output (one line):

```
"<transcribed text>"\t<confidence>
```

Example:

```
"Hello world, this is a code review."	0.6636
```

Supported input formats: **WebM/Opus** (browser default), **WAV**, **M4A**.

## Latency (rough benchmarks, Apple M-series CPU)

| Model | 5-second clip | First run (includes load) |
|-------|--------------|--------------------------|
| `tiny` | ~0.4 s | ~1.5 s |
| `base` | ~0.8 s | ~2.5 s |

Measured on Apple M3 with `compute_type=int8` (default). Load time is a one-time cost per process; the adapter caches the model in memory.

## Confidence heuristic

faster-whisper returns per-segment `avg_logprob` and `no_speech_prob`.

We aggregate to a single [0, 1] value:

```
segment_conf_i  = exp(avg_logprob_i)         # maps logprob → (0, 1]
mean_conf       = mean(segment_conf_i)        # average across segments
final_conf      = mean_conf × (1 – max(no_speech_prob_i))
```

- `exp(avg_logprob)` converts token log-probability to a linear scale.
- Averaging across segments is more robust than min/max for variable-length audio.
- Multiplying by `(1 – max_no_speech_prob)` deflates confidence when Whisper suspects silence or noise.

## Architecture

```
STTAdapter.transcribe(audio: bytes, mime: str) -> (str, float)
    │
    ├── audio.py: decode_to_float32()
    │     pydub reads WebM/Opus/WAV/M4A via ffmpeg
    │     → resample to 16 kHz mono float32
    │
    └── adapter.py: WhisperSTTAdapter._transcribe_sync()
          faster_whisper.WhisperModel.transcribe()
          → aggregate segments → (text, confidence)
```

The `transcribe()` method is `async` and runs the synchronous faster-whisper call in `asyncio.to_thread()` — safe to await in any FastAPI route.

## Running tests

```bash
cd backend/
pytest tests/stt/ -v
```

Tests included:

| Test | What it checks |
|------|---------------|
| `test_transcribe_keywords` | Transcription contains words from the fixture audio |
| `test_confidence_range` | Confidence ∈ [0, 1] |
| `test_confidence_above_threshold` | Confidence > 0.5 for clean TTS speech |
| `test_no_network_calls` | `socket.socket` is never called during transcription (compliance) |
| `test_tuple_return_type` | Return value matches the `STTAdapter` protocol signature |
