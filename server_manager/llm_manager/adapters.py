"""Adapters re-exporting the existing proven LLM Manager modules (§5 strangler).

The legacy import paths (service/app/*) remain the live implementations.
Each adapter here re-exports it so new code imports via server_manager.*;
when a module is later moved, only these adapters change.
"""
from __future__ import annotations

import importlib
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_APP_DIR = os.path.join(_REPO_ROOT, "service", "app")


def _reexport(module_name: str, names: list[str]) -> object:
    mod = importlib.import_module(module_name)
    return mod


def _ensure_app_on_path() -> None:
    if _APP_DIR not in sys.path:
        sys.path.insert(0, _APP_DIR)


_ensure_app_on_path()

# Lazy adapter objects: attribute access resolves into the legacy module so the
# existing single-file app keeps ownership of its own constants (no import-time
# side effects beyond what the module already performs today).
class _LegacyAdapter:
    def __init__(self, module_name: str):
        self._module_name = module_name
        self._module = None

    def _load(self):
        if self._module is None:
            _ensure_app_on_path()
            self._module = importlib.import_module(self._module_name)
        return self._module

    def __getattr__(self, name):
        return getattr(self._load(), name)


v011_core = _LegacyAdapter("v011_core")
active_hosts = _LegacyAdapter("active_deployments")
litellm_sync = _LegacyAdapter("litellm_sync")
recovery = _LegacyAdapter("v011_recovery")
spend = _LegacyAdapter("spend_guard")
