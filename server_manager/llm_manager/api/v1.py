"""Server Manager API v1 — LLM Manager routes (model-routes + usage), §10.

Machine API (distinct from human UI / A2A). Scoped bearer identity required.
Model routes + usage read from the EXISTING llmmanager DB (strangler: read
only this sprint; writes stay in the proven app).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from server_manager.common.auth.service_tokens import require_identity
from server_manager.common.db.session import get_session_factory

router = APIRouter(prefix="/api/v1", tags=["server-manager-v1-llm"])

API_CONTRACT_VERSION = "1.0.0"


@router.get("/model-routes/{route}")
def get_model_route(route: str, request: Request):
    """A model route = a row of model_registry (+ its backends). Read-only."""
    require_identity(request, "route:read")
    Session = get_session_factory("llm")
    with Session() as session:
        from sqlalchemy import text

        row = session.execute(
            text("SELECT logical_model_name, engine, backend_url, context_limit, health, routable "
                 "FROM model_registry WHERE logical_model_name = :m"),
            {"m": route},
        ).mappings().all()
        if not row:
            raise HTTPException(status_code=404, detail=f"no model route {route!r}")
        return {"route": route, "api_contract_version": API_CONTRACT_VERSION,
                "backends": [dict(r) for r in row]}


@router.get("/usage")
def usage(request: Request, hours: int = 24):
    """Aggregated token usage from LiteLLM_SpendLogs (read-only, §10)."""
    require_identity(request, "usage:read")
    if hours < 1 or hours > 24 * 30:
        raise HTTPException(status_code=422, detail="hours must be 1..720")
    Session = get_session_factory("litellm")
    with Session() as session:
        from sqlalchemy import text

        rows = session.execute(text(
            """
            SELECT model,
                   count(*)                     AS requests,
                   COALESCE(sum(prompt_tokens),0)     AS prompt_tokens,
                   COALESCE(sum(completion_tokens),0) AS completion_tokens
            FROM "LiteLLM_SpendLogs"
            WHERE "startTime" >= now() - make_interval(hours => :h)
            GROUP BY model
            ORDER BY requests DESC
            LIMIT 50
            """
        ), {"h": hours}).mappings().all()
        return {"window_hours": hours, "api_contract_version": API_CONTRACT_VERSION,
                "models": [dict(r) for r in rows]}


# ------------------------------------------------------------------ ORM reads
# TDR-0008 slice 1: reflective ORM models over hosts/active_hosts/model_registry.
# Read-parity path — same data as the Core text() queries, through ORM models.
@router.get("/hosts")
def list_hosts(request: Request):
    require_identity(request, "route:read")
    from server_manager.llm_manager.models.orm import ActiveHostRecord as ActiveRecord
    from server_manager.llm_manager.models.orm import HostRecord

    Session = get_session_factory("llm")
    with Session() as session:
        hosts = session.query(HostRecord).order_by(HostRecord.host_id).all()
        active = {a.physical_host: a.active_host_ip for a in session.query(ActiveRecord).all()}
        # hosts.name carries a VM suffix ('MIAM-00111 / VM102 (Flash-Next PROD)');
        # active_hosts keys are the bare MIAM-##### asset id → match on the first token.
        import re as _re

        def _asset(name: str) -> str | None:
            m = _re.match(r"(MIAM-\d{5})", name or "")
            return m.group(1) if m else None

        return {"api_contract_version": API_CONTRACT_VERSION, "hosts": [
            {"host_id": h.host_id, "name": h.name, "guest_ip": h.guest_ip,
             "gpu_count": h.gpu_count, "gpu_model": h.gpu_model, "node": h.node,
             "vmid": h.vmid, "desired_power_state": h.desired_power_state,
             "desired_service_state": h.desired_service_state,
             "active_model_host": active.get(_asset(h.name))}
            for h in hosts
        ], "active_hosts": [{"physical_host": k, "active_host_ip": v} for k, v in active.items()]}


@router.get("/model-routes")
def list_model_routes(request: Request):
    require_identity(request, "route:read")
    from server_manager.llm_manager.models.orm import ModelRegistryRecord

    Session = get_session_factory("llm")
    with Session() as session:
        rows = session.query(ModelRegistryRecord).order_by(ModelRegistryRecord.logical_model_name).all()
        routes: dict[str, list[dict]] = {}
        for r in rows:
            routes.setdefault(r.logical_model_name, []).append({
                "engine": r.engine, "backend_url": r.backend_url,
                "context_limit": r.context_limit, "health": r.health,
                "routable": r.routable, "is_alias": r.is_alias,
                "alias_target": r.alias_target,
            })
        return {"api_contract_version": API_CONTRACT_VERSION, "routes": routes,
                "count": len(routes)}