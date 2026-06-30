"""FastAPI dependency that provides AppContext to routes.

The singleton is created lazily on first use. Tests call set_app_context()
before issuing requests; the startup hook in main.py is a no-op when a
context is already set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pr_walkthrough.orchestration import AppContext

# Module-level singleton; replaced by tests via set_app_context()
_app_context: "AppContext | None" = None


def get_app_context() -> "AppContext":
    global _app_context
    if _app_context is None:
        import os
        from pathlib import Path
        from pr_walkthrough.orchestration import AppContext

        # Parent directory holding repo checkouts as subdirs. Defaults to
        # ~/code; override via PR_WALKTHROUGH_REPOS_DIR. The active repo
        # for a session is resolved per-PR from the URL slug — one backend
        # process can walk PRs from any repo under this parent dir.
        repos_dir = Path(
            os.environ.get(
                "PR_WALKTHROUGH_REPOS_DIR",
                str(Path.home() / "code"),
            )
        ).expanduser()
        # Opt-in persistent narration + TTS cache (driven by the user's
        # global config via the CLI). Legacy `uvicorn`-only launches
        # without the env var get the previous behaviour: no cache.
        cache_obj = None
        if os.environ.get("PR_WALKTHROUGH_CACHE"):
            try:
                from pr_walkthrough.cache import PersistentCache
                max_gb = float(
                    os.environ.get("PR_WALKTHROUGH_CACHE_MAX_GB", "1") or "1"
                )
                cache_obj = PersistentCache(max_bytes=int(max_gb * 1_073_741_824))
            except Exception:  # pragma: no cover - defensive
                cache_obj = None
        _app_context = AppContext(repos_dir=repos_dir, cache=cache_obj)
    return _app_context


def set_app_context(ctx: "AppContext") -> None:
    """Inject a custom context (used by tests and main.py custom wiring)."""
    global _app_context
    _app_context = ctx


def reset_app_context() -> None:
    """Reset to None so next call to get_app_context() creates a fresh default."""
    global _app_context
    _app_context = None
