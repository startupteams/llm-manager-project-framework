"""PDU consumer integration for Server Manager (REV4 §10C; flight plan Phase 4).

High-level, asset-addressed power functions over the PDU Manager /api/v1 via
the reusable client (``pdu_manager_client`` vendored from the PDU repo).

Rules baked in (PDU repo AGENTS.md §12 + plan §10C):
- Asset identity FIRST: every action names the MIAM-##### asset id; the
  expected_asset_id guard (409 TARGET_IDENTITY_MISMATCH) is always sent.
- Read-only + dry-run are safe by default. ACTUATION is gated: this module
  exposes plan/ensure/request functions, but the API layer only exposes
  read-only + dry-run endpoints unless ``SERVER_MANAGER_PDU_ACTUATION=1``.
- Correlation metadata passthrough (ACMS Work → SM op → PDU job/audit).
- Credentials from the external 0600 file only, never Git.
- PDU ON != server ready: callers must re-check OS/runtime state after any
  power action (see reconcile-driven flows).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache

from server_manager.common.config.settings import get_settings


def _load_pdu_creds() -> tuple[str, str]:
    path = os.environ.get("SERVER_MANAGER_PDU_CREDS_FILE", "/etc/llm-manager/secrets/pdu_agent_creds")
    try:
        with open(path) as f:
            d = json.load(f)
        return d.get("user", ""), d.get("password", "")
    except (OSError, ValueError):
        return "", ""


@lru_cache(maxsize=1)
def _client():
    from server_manager.common.pdu_manager_client import PduManagerClient

    s = get_settings()
    base = os.environ.get("SERVER_MANAGER_PDU_BASE_URL", "https://10.0.20.156/api/v1")
    user, pw = _load_pdu_creds()
    if not (user and pw):
        raise RuntimeError("PDU credentials not configured")
    # PDU Manager serves a self-signed cert on the private network; the pinned
    # DER fingerprint lives in deployment docs. verify=False is an explicit,
    # documented opt-out here (never a silent default).
    return PduManagerClient(base, user=user, password=pw, verify=False)


# ------------------------------------------------------------------- reads
def pdu_health() -> dict:
    return _client().health()


def pdu_capabilities() -> dict:
    return _client().capabilities()


def list_assets() -> dict:
    return _client().list_assets()


def get_power_state(asset_id: str) -> dict:
    """Asset power state + identity resolution (read-only)."""
    c = _client()
    state = c.get_power_state(asset_id)
    asset = c.get_asset(asset_id)
    return {"asset_id": asset_id, "power_state": state, "asset": asset}


def pdu_job(job_id: str) -> dict:
    return _client().get_job(job_id)


def pdu_audit(lines: int = 40) -> list:
    return _client().audit(lines=lines)


# ------------------------------------------------------------ dry-run plans
def plan_power_action(asset_id: str, action: str) -> dict:
    """Dry-run: what WOULD happen (target resolution, current state, policy,
    required acks, caller authz). Never actuates."""
    return _client().plan_action(asset_id=asset_id, action=action)


# --------------------------------------------- coordinated actuation (gated)
def actuation_enabled() -> bool:
    """Physical ON/OFF/REBOOT requires the env flag AND exact asset/action
    authorization in the execution context (plan §10C; PDU repo §12)."""
    return os.environ.get("SERVER_MANAGER_PDU_ACTUATION", "") == "1"


class ActuationBlocked(RuntimeError):
    pass


def ensure_powered_on(asset_id: str, reason: str, request_id: str,
                      correlation_id: str | None = None,
                      source_service: str = "svc-server-manager") -> dict:
    """Power ON an asset if it is OFF (PDU ON != server ready)."""
    if not actuation_enabled():
        raise ActuationBlocked("PDU actuation disabled (SERVER_MANAGER_PDU_ACTUATION != 1)")
    plan = plan_power_action(asset_id, "on")
    if plan.get("current_state") == "ON":
        return {"action": "none_needed", "asset_id": asset_id, "state": "ON"}
    return _client().submit_action(asset_id=asset_id, action="on", reason=reason,
                                   request_id=request_id, correlation_id=correlation_id,
                                   source_service=source_service,
                                   expected_asset_id=asset_id)


def request_reboot(asset_id: str, reason: str, request_id: str,
                   correlation_id: str | None = None,
                   source_service: str = "svc-server-manager") -> dict:
    if not actuation_enabled():
        raise ActuationBlocked("PDU actuation disabled (SERVER_MANAGER_PDU_ACTUATION != 1)")
    return _client().submit_action(asset_id=asset_id, action="reboot", reason=reason,
                                   request_id=request_id, correlation_id=correlation_id,
                                   source_service=source_service,
                                   expected_asset_id=asset_id)
