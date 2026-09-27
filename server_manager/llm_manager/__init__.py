"""Server Manager — LLM Manager adapter layer (Phase 1 strangler, staged).

Today: re-exports the proven existing modules so new consumers import through
server_manager.* while the legacy paths keep working (§5: route behavior
through the new module, remove the old path only after parity).
Next stages move the implementations here one module at a time.
"""
from __future__ import annotations

# --- routing / recovery adapters (existing proven code) ---
from server_manager.llm_manager.adapters import (  # noqa: F401
    active_hosts,
    litellm_sync,
    recovery,
    spend,
    v011_core,
)

__all__ = ["active_hosts", "litellm_sync", "recovery", "spend", "v011_core"]
