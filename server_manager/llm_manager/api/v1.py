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