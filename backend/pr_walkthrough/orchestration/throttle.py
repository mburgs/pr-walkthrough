"""Adjustable concurrency caps for the two expensive things the worker
does per chunk: an LLM call (API-bound, light memory) and a TTS synth
(CPU-bound, ~200 MB resident per kokoro session + audio buffers).

Both caps are exposed as semaphores hung off `AppContext`. The chunk
worker awaits each semaphore before doing the relevant work, so the
parallel prefetch in multi-level mode (4 levels × N chunks) can't
spawn a herd that swamps the box.

Defaults
--------
- TTS concurrency auto-scales with total system RAM:
    < 16 GB → 1   (most laptops)
    < 32 GB → 2
    >= 32 GB → 4
  Best-effort; if RAM can't be detected we conservatively pick 1.
- LLM concurrency defaults to 8. The Anthropic SDK and tier limits
  typically handle far more, but bounded parallelism makes plan→narrate
  scheduling predictable and keeps a runaway loop from melting credits.

Overrides
---------
Both are overridable via env at process start:
    PR_WALKTHROUGH_TTS_CONCURRENCY   integer ≥ 1
    PR_WALKTHROUGH_LLM_CONCURRENCY   integer ≥ 1

Concurrency-model caveats
-------------------------
The semaphores are `asyncio.Semaphore` instances on AppContext. That makes
them **per-process** caps, correct within a single uvicorn event loop:

  - Single-worker uvicorn (`uvicorn …app`)         → cap is global. ✅
  - Multi-worker uvicorn (`uvicorn … --workers N`) → each worker has its
    own AppContext + own semaphore. Effective parallelism is N × cap.
    Divide your target by the worker count, or run single-worker.
  - TTS adapters that spawn subprocesses (XTTS in some configs) bypass
    the semaphore. Kokoro doesn't — torch inference is in-process.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)


def _detect_total_ram_gb() -> int:
    """Return total system RAM in gigabytes, or 0 if undetectable.

    Uses stdlib only — `sysctl` on macOS, `/proc/meminfo` on Linux. Any
    error (subprocess failure, missing file, parse error) returns 0,
    which the caller treats as "be conservative".
    """
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], timeout=2,
            )
            return int(out.strip()) // (1024 ** 3)
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb // (1024 ** 2)
    except Exception:
        log.info("RAM detection failed; assuming small machine", exc_info=True)
    return 0


def _auto_tts_concurrency() -> int:
    ram = _detect_total_ram_gb()
    if ram >= 32:
        return 4
    if ram >= 16:
        return 2
    return 1  # ≤16 GB or undetectable


def resolve_tts_concurrency() -> int:
    """Effective TTS-synth parallelism cap. Env override > auto-detect."""
    raw = os.environ.get("PR_WALKTHROUGH_TTS_CONCURRENCY")
    if raw is not None:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
        log.warning("ignored invalid PR_WALKTHROUGH_TTS_CONCURRENCY=%r", raw)
    return _auto_tts_concurrency()


def resolve_llm_concurrency() -> int:
    """Effective LLM-call parallelism cap. Env override > default 8."""
    raw = os.environ.get("PR_WALKTHROUGH_LLM_CONCURRENCY")
    if raw is not None:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
        log.warning("ignored invalid PR_WALKTHROUGH_LLM_CONCURRENCY=%r", raw)
    return 8
