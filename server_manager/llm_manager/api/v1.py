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


# ------------------------------------------------------ ORM slices 2–3 (TDR-0008)
@router.get("/deployment-revisions")
def list_deployment_revisions(request: Request, model: str | None = None,
                              limit: int = 100):
    """Deployment/revision history (slice 2). Read-only; rollback payloads
    (launch_command, systemd_unit) are included — this endpoint is the machine
    view of 'capture exact rollback configuration before replacing a runtime'."""
    require_identity(request, "route:read")
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    from server_manager.llm_manager.models.orm import DeploymentRevisionRecord

    Session = get_session_factory("llm")
    with Session() as session:
        q = session.query(DeploymentRevisionRecord)
        if model:
            q = q.filter(DeploymentRevisionRecord.model == model)
        rows = q.order_by(DeploymentRevisionRecord.revision_id.desc()).limit(limit).all()
        return {"api_contract_version": API_CONTRACT_VERSION, "revisions": [
            {"revision_id": r.revision_id, "revision_label": r.revision_label,
             "model": r.model, "artifact": r.artifact,
             "artifact_revision": r.artifact_revision, "quantization": r.quantization,
             "runtime": r.runtime, "runtime_version": r.runtime_version,
             "host": r.host, "vm": r.vm, "guest_ip": r.guest_ip,
             "gpu_model": r.gpu_model, "gpu_count": r.gpu_count,
             "context": r.context, "parallelism": r.parallelism,
             "max_num_seqs": r.max_num_seqs, "kv_cache": r.kv_cache,
             "launch_command": r.launch_command, "systemd_unit": r.systemd_unit,
             "aliases": r.aliases, "status": r.status,
             "visible_by_default": r.visible_by_default, "preset_id": r.preset_id,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]}


@router.get("/deployments")
def list_deployments(request: Request):
    """Current serving deployments per host (slice 2)."""
    require_identity(request, "route:read")
    from server_manager.llm_manager.models.orm import DeploymentRecord

    Session = get_session_factory("llm")
    with Session() as session:
        rows = session.query(DeploymentRecord).order_by(DeploymentRecord.deployment_id).all()
        return {"api_contract_version": API_CONTRACT_VERSION, "deployments": [
            {"deployment_id": d.deployment_id, "host_id": d.host_id,
             "served_model": d.served_model, "max_model_len": d.max_model_len,
             "enabled": d.enabled, "config_version": d.config_version,
             "desired_service_state": d.desired_service_state}
            for d in rows]}


@router.get("/benchmarks")
def list_benchmarks(request: Request, model: str | None = None,
                    revision_id: int | None = None, limit: int = 50):
    """Benchmark runs WITH deployment context (slice 3). Failed runs included
    (status != COMPLETED surfaces as-is) — never hide failures (repo policy §5)."""
    require_identity(request, "route:read")
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    from server_manager.llm_manager.models.orm import BenchmarkRunRecord

    Session = get_session_factory("llm")
    with Session() as session:
        q = session.query(BenchmarkRunRecord)
        if revision_id is not None:
            q = q.filter(BenchmarkRunRecord.deployment_revision_id == revision_id)
        if model:
            # link through the revision's model when filtering by model name
            from server_manager.llm_manager.models.orm import DeploymentRevisionRecord

            q = (q.join(DeploymentRevisionRecord,
                        BenchmarkRunRecord.deployment_revision_id == DeploymentRevisionRecord.revision_id)
                 .filter(DeploymentRevisionRecord.model == model))
        rows = q.order_by(BenchmarkRunRecord.run_id.desc()).limit(limit).all()
        def _num(v):
            return float(v) if v is not None else None
        return {"api_contract_version": API_CONTRACT_VERSION, "runs": [
            {"run_id": r.run_id, "ts": r.ts.isoformat() if r.ts else None,
             "preset": r.preset, "concurrency": r.concurrency,
             "requests": r.requests, "ok": r.ok, "failed": r.failed,
             "duration_s": _num(r.duration_s), "output_tok_s": _num(r.output_tok_s),
             "ttft_avg_s": _num(r.ttft_avg_s), "ttft_max_s": _num(r.ttft_max_s),
             "latency_avg_s": _num(r.latency_avg_s),
             "gpu_watts_avg": _num(r.gpu_watts_avg), "target": r.target,
             "deployment_revision_id": r.deployment_revision_id,
             "prompt_tokens": r.prompt_tokens, "output_tokens": r.output_tokens,
             "aggregate_output_tok_s": _num(r.aggregate_output_tok_s),
             "ram_used_gb": _num(r.ram_used_gb), "vram_used_gb": _num(r.vram_used_gb),
             "tested_context": r.tested_context, "raw_result_path": r.raw_result_path,
             "hermes_tool_pass": r.hermes_tool_pass, "status": r.status}
            for r in rows]}


@router.get("/qualification-envelopes")
def list_qualification_envelopes(request: Request, model: str | None = None):
    """Qualified concurrency/context envelopes (slice 3)."""
    require_identity(request, "route:read")
    from server_manager.llm_manager.models.orm import QualificationEnvelopeRecord

    Session = get_session_factory("llm")
    with Session() as session:
        q = session.query(QualificationEnvelopeRecord)
        if model:
            q = q.filter(QualificationEnvelopeRecord.model == model)
        rows = q.order_by(QualificationEnvelopeRecord.envelope_id.desc()).limit(200).all()
        return {"api_contract_version": API_CONTRACT_VERSION, "envelopes": [
            {"envelope_id": e.envelope_id, "deployment_ip": e.deployment_ip,
             "model": e.model, "recommended_concurrency": e.recommended_concurrency,
             "hard_concurrency_limit": e.hard_concurrency_limit,
             "qualified_context": e.qualified_context,
             "benchmark_run_id": e.benchmark_run_id,
             "kv_cache_dtype": e.kv_cache_dtype, "notes": e.notes,
             "ts": e.ts.isoformat() if e.ts else None}
            for e in rows]}