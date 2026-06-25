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

        # Allow the user to point context retrieval at a cloned repo without
        # having to edit code. Useful when running the demo against a real PR
        # whose codebase sits outside the orchestrator's own cwd.
        repo_root = Path(os.environ.get("PR_WALKTHROUGH_REPO_ROOT", "."))
        _app_context = AppContext(repo_root=repo_root)
    return _app_context


def set_app_context(ctx: "AppContext") -> None:
    """Inject a custom context (used by tests and main.py custom wiring)."""
    global _app_context
    _app_context = ctx


def reset_app_context() -> None:
    """Reset to None so next call to get_app_context() creates a fresh default."""
    global _app_context
    _app_context = None
