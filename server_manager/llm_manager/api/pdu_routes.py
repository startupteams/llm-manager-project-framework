"""PDU routes for Server Manager machine API v1 (REV4 §10C; flight plan Phase 4).

Read-only + dry-run only by default; actuation requires SERVER_MANAGER_PDU_ACTUATION=1
AND exact asset/action authorization in the execution context. Correlation
metadata (correlation_id / source_service / upstream_operation_id) passes through
to PDU audit records so ACMS Work → SM op → PDU job chains stay traceable.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from server_manager.common.auth.service_tokens import ServiceIdentity
from server_manager.common.pdu_power import (
    ActuationBlocked,
    ensure_powered_on,
    pdu_audit,
    pdu_capabilities,
    pdu_health,
    pdu_job,
    get_power_state,
    list_assets,
    plan_power_action,
    request_reboot,
)

router = APIRouter(prefix="/api/v1/pdu", tags=["server-manager-pdu"])


def _auth(scope: str):
    def dep(request: Request) -> ServiceIdentity:
        from server_manager.common.auth.service_tokens import require_identity

        return require_identity(request, scope)
    return dep


def _pdu_error(e: Exception) -> HTTPException:
    """Map PDU client exceptions to honest HTTP status codes."""
    status = getattr(e, "status_code", None)
    if status == 401:
        return HTTPException(status_code=502, detail=f"PDU auth failure: {e}")
    if status == 409:
        return HTTPException(status_code=409, detail=f"PDU identity mismatch: {e}")
    return HTTPException(status_code=502, detail=f"PDU error: {e}")


@router.get("/health")
def pdu_health_route(ident: ServiceIdentity = Depends(_auth("health:read"))):
    try:
        return pdu_health()
    except Exception as e:  # noqa: BLE001
        raise _pdu_error(e)


@router.get("/capabilities")
def pdu_capabilities_route(ident: ServiceIdentity = Depends(_auth("health:read"))):
    try:
        return pdu_capabilities()
    except Exception as e:  # noqa: BLE001
        raise _pdu_error(e)


@router.get("/assets")
def pdu_assets_route(ident: ServiceIdentity = Depends(_auth("health:read"))):
    try:
        return list_assets()
    except Exception as e:  # noqa: BLE001
        raise _pdu_error(e)


@router.get("/assets/{asset_id}/power-state")
def pdu_power_state_route(asset_id: str, ident: ServiceIdentity = Depends(_auth("health:read"))):
    try:
        return get_power_state(asset_id)
    except Exception as e:  # noqa: BLE001
        raise _pdu_error(e)


class ActionPlanRequest(BaseModel):
    asset_id: str = Field(min_length=6)
    action: str = Field(min_length=2, max_length=16)


@router.post("/action-plans")
def pdu_plan_route(body: ActionPlanRequest, ident: ServiceIdentity = Depends(_auth("health:read"))):
    """Dry-run plan (actuated=false always). Use to pre-verify identity/policy."""
    try:
        return plan_power_action(body.asset_id, body.action)
    except Exception as e:  # noqa: BLE001
        raise _pdu_error(e)


@router.get("/jobs/{job_id}")
def pdu_job_route(job_id: str, ident: ServiceIdentity = Depends(_auth("job:read"))):
    try:
        return pdu_job(job_id)
    except Exception as e:  # noqa: BLE001
        raise _pdu_error(e)


@router.get("/audit")
def pdu_audit_route(lines: int = 40, ident: ServiceIdentity = Depends(_auth("health:read"))):
    try:
        return {"lines": pdu_audit(lines=min(lines, 200))}
    except Exception as e:  # noqa: BLE001
        raise _pdu_error(e)


# -------------------------------------------------- actuation (gated, audited)
class PowerActionRequest(BaseModel):
    asset_id: str = Field(min_length=6)
    action: str = Field(min_length=2, max_length=16)  # on | reboot (off refused for protected)
    reason: str = Field(min_length=4)
    request_id: str = Field(min_length=8)
    correlation_id: str | None = None
    source_service: str = "svc-server-manager"


@router.post("/actions")
def pdu_action_route(body: PowerActionRequest, ident: ServiceIdentity = Depends(_auth("job:submit"))):
    """Gated physical actuation. Requires SERVER_MANAGER_PDU_ACTUATION=1; every
    call here MUST already carry human authorization for the exact asset/action
    in its execution context (plan §10C)."""
    if body.action == "off":
        raise HTTPException(status_code=403, detail="OFF via SM is refused; protected-outlet policy forbids it (use PDU Manager directly with authorized flow)")
    try:
        if body.action == "on":
            res = ensure_powered_on(body.asset_id, body.reason, body.request_id,
                                    correlation_id=body.correlation_id,
                                    source_service=body.source_service)
        elif body.action == "reboot":
            res = request_reboot(body.asset_id, body.reason, body.request_id,
                                 correlation_id=body.correlation_id,
                                 source_service=body.source_service)
        else:
            raise HTTPException(status_code=422, detail=f"unsupported action {body.action}")
    except ActuationBlocked as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise _pdu_error(e)
    return res
