"""Agent Runtime Manager API — Server Manager machine API v1 (§10).

Infrastructure API distinct from A2A. All endpoints require a scoped
service identity (§11). DELETE applies ONLY to Runtime-Manager-owned
runtimes (§8) and never means "delete ACMS persistent agent".
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from server_manager.agent_runtime_manager.models.orm import (
    ActualState,
    AgentRuntime,
    JobState,
    ProvisioningJob,
    RuntimeEvent,
    RuntimeState,
)
from server_manager.agent_runtime_manager.providers.proxmox_vm import (
    OwnershipError,
    ProxmoxVMProvider,
    verify_ownership,
)
from server_manager.agent_runtime_manager.services.provisioning import ProvisionRequest, ProvisioningService
from server_manager.common.auth.service_tokens import require_identity
from server_manager.common.db.session import get_session_factory

router = APIRouter(prefix="/api/v1", tags=["server-manager-v1"])

from server_manager.common.auth.service_tokens import ServiceIdentity  # noqa: E402


def _auth(scope: str):
    """Dependency factory: identity check runs BEFORE body validation (401-first)."""
    def dep(request: Request) -> ServiceIdentity:
        return require_identity(request, scope)
    return dep



API_CONTRACT_VERSION = "1.0.0"


# --------------------------------------------------------------------- schemas
class CreateRuntimeRequest(BaseModel):
    acms_agent_id: str = Field(min_length=8, max_length=64)
    request_id: str = Field(min_length=8, max_length=64)  # idempotency key
    name: str = Field(min_length=3, max_length=63)
    harness: str = "hermes"
    runtime_class: str = "software_development_worker"
    site: str = "marion-ia-usa"
    model_route: str | None = None
    bridge_profile: dict[str, Any] | None = None
    desired_state: str = "DESIRED_RUNNING"


class DesiredStateRequest(BaseModel):
    desired_state: str
    reason: str = Field(min_length=4)


class ReconcileOut(BaseModel):
    runtime_id: str
    desired: str
    before: str
    after: str
    action: str
    error: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class StateSyncReport(BaseModel):
    runtime_id: str
    hermes_state_repo: str | None
    hermes_state_branch: str | None
    hermes_profile_name: str | None
    last_state_commit_sha: str | None
    state_sync_health: str | None
    reported_by: str
    observed_at: str


class RuntimeOut(BaseModel):
    runtime_id: str
    acms_agent_id: str
    provisioning_request_id: str
    name: str
    provider: str
    node: str | None
    vmid: int | None
    harness: str
    runtime_class: str
    site: str
    model_route: str | None
    desired_state: str
    actual_state: str
    created_by_agent_runtime_manager: bool
    hermes_state_repo: str | None
    hermes_state_branch: str | None
    hermes_profile_name: str | None
    last_state_commit_sha: str | None
    state_sync_health: str | None
    last_reconcile_at: str | None
    last_error: str | None
    recovery_count: int
    api_contract_version: str = API_CONTRACT_VERSION


class JobOut(BaseModel):
    job_id: str
    request_id: str
    runtime_id: str
    state: str
    error: str | None
    authority: str
    steps: list[dict[str, Any]]


def _runtime_out(r: AgentRuntime) -> RuntimeOut:
    return RuntimeOut(
        runtime_id=str(r.runtime_id), acms_agent_id=r.acms_agent_id,
        provisioning_request_id=r.provisioning_request_id, name=r.name,
        provider=r.provider, node=r.node, vmid=r.vmid, harness=r.harness,
        runtime_class=r.runtime_class, site=r.site, model_route=r.model_route,
        desired_state=r.desired_state.value, actual_state=r.actual_state.value,
        created_by_agent_runtime_manager=r.created_by_agent_runtime_manager,
        hermes_state_repo=r.hermes_state_repo, hermes_state_branch=r.hermes_state_branch,
        hermes_profile_name=r.hermes_profile_name, last_state_commit_sha=r.last_state_commit_sha,
        state_sync_health=r.state_sync_health,
        last_reconcile_at=r.last_reconcile_at.isoformat() if r.last_reconcile_at else None,
        last_error=r.last_error,
        recovery_count=r.recovery_count or 0,
    )


# ----------------------------------------------------------------- endpoints
@router.post("/agent-runtimes", status_code=202)
def create_agent_runtime(body: CreateRuntimeRequest, ident: ServiceIdentity = Depends(_auth("runtime:write"))):
    Session = get_session_factory("arm")
    svc = ProvisioningService()
    with Session() as session:
        req = ProvisionRequest(
            acms_agent_id=body.acms_agent_id, request_id=body.request_id, name=body.name,
            harness=body.harness, runtime_class=body.runtime_class, site=body.site,
            model_route=body.model_route, bridge_profile=body.bridge_profile,
            desired_state=RuntimeState(body.desired_state),
        )
        job, created = svc.submit(session, req, requested_by=ident.service, authority="sprint_execution_context")
        if created:
            # run synchronously for now (single-worker MVP); jobs can also be drained by a loop
            svc.run_job(session, job.job_id)
            session.refresh(job)
    return {"job": JobOut(
        job_id=str(job.job_id), request_id=job.request_id, runtime_id=str(job.runtime_id),
        state=job.state.value, error=job.error, authority=job.authority,
        steps=[{"seq": st.seq, "step": st.step, "state": st.state, "detail": st.detail} for st in job.steps],
    ), "created": created}


@router.get("/provisioning-jobs/{job_id}")
def get_provisioning_job(job_id: str, ident: ServiceIdentity = Depends(_auth("job:read"))):
    Session = get_session_factory("arm")
    with Session() as session:
        job = session.get(ProvisioningJob, uuid.UUID(job_id))
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return JobOut(
            job_id=str(job.job_id), request_id=job.request_id, runtime_id=str(job.runtime_id),
            state=job.state.value, error=job.error, authority=job.authority,
            steps=[{"seq": st.seq, "step": st.step, "state": st.state, "detail": st.detail} for st in job.steps],
        )


@router.get("/agent-runtimes/{runtime_id}")
def get_agent_runtime(runtime_id: str, ident: ServiceIdentity = Depends(_auth("runtime:read"))):
    Session = get_session_factory("arm")
    with Session() as session:
        r = session.get(AgentRuntime, uuid.UUID(runtime_id))
        if r is None:
            raise HTTPException(status_code=404, detail="runtime not found")
        return _runtime_out(r)


@router.get("/agent-runtimes")
def list_agent_runtimes(ident: ServiceIdentity = Depends(_auth("runtime:read")), acms_agent_id: str | None = None):
    Session = get_session_factory("arm")
    with Session() as session:
        q = select(AgentRuntime)
        if acms_agent_id:
            q = q.where(AgentRuntime.acms_agent_id == acms_agent_id)
        rows = session.execute(q.order_by(AgentRuntime.created_at.desc())).scalars().all()
        return {"runtimes": [_runtime_out(r).model_dump() for r in rows], "count": len(rows)}


@router.post("/agent-runtimes/{runtime_id}/desired-state")
def set_desired_state(runtime_id: str, body: DesiredStateRequest, ident: ServiceIdentity = Depends(_auth("runtime:write"))):
    Session = get_session_factory("arm")
    with Session() as session:
        r = session.get(AgentRuntime, uuid.UUID(runtime_id))
        if r is None:
            raise HTTPException(status_code=404, detail="runtime not found")
        try:
            r.desired_state = RuntimeState(body.desired_state)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"unknown desired state {body.desired_state}")
        session.add(RuntimeEvent(actor=ident.service, action="desired_state_set", runtime_id=r.runtime_id,
                                 detail={"desired": body.desired_state, "reason": body.reason}))
        session.commit()
        return _runtime_out(r)


@router.delete("/agent-runtimes/{runtime_id}", status_code=200)
def destroy_agent_runtime(runtime_id: str, ident: ServiceIdentity = Depends(_auth("runtime:destroy"))):
    """Destroy a Runtime-Manager-owned runtime VM (§8). NEVER deletes ACMS
    persistent agent identity/history."""
    s = __import__("server_manager.common.config.settings", fromlist=["get_settings"]).get_settings()
    Session = get_session_factory("arm")
    provider = ProxmoxVMProvider()
    with Session() as session:
        r = session.get(AgentRuntime, uuid.UUID(runtime_id))
        if r is None:
            raise HTTPException(status_code=404, detail="runtime not found")
        if r.vmid is None or r.node is None:
            raise HTTPException(status_code=409, detail="runtime has no backing VM")
        try:
            verify_ownership(provider, r.node, r.vmid, r, s.protected_vmids)
        except OwnershipError as e:
            session.add(RuntimeEvent(actor=ident.service, action="destroy_refused", runtime_id=r.runtime_id,
                                     result="refused", detail={"reason": str(e)}))
            session.commit()
            raise HTTPException(status_code=403, detail=f"destroy refused: {e}")
        # audit BEFORE the destructive act (§8 order)
        session.add(RuntimeEvent(actor=ident.service, action="destroy_audit", runtime_id=r.runtime_id,
                                 detail={"vmid": r.vmid, "node": r.node}))
        session.commit()
        provider.shutdown(r.node, r.vmid)
        provider.destroy(r.node, r.vmid)
        r.actual_state = ActualState.DESTROYED
        r.desired_state = RuntimeState.DESIRED_DESTROYED
        r.vmid = None
        r.node = None
        session.add(RuntimeEvent(actor=ident.service, action="destroy_complete", runtime_id=r.runtime_id,
                                 detail={"note": "ACMS persistent identity/history NOT deleted"}))
        session.commit()
        return {"status": "DESTROYED", "runtime_id": runtime_id,
                "note": "runtime destroyed; ACMS persistent identity/history preserved"}


@router.get("/capabilities")
def capabilities(ident: ServiceIdentity = Depends(_auth("health:read"))):
    return {
        "api": "server-manager", "api_contract_version": API_CONTRACT_VERSION,
        "endpoints": [
            "POST /api/v1/agent-runtimes", "GET /api/v1/provisioning-jobs/{job_id}",
            "GET /api/v1/agent-runtimes/{runtime_id}", "GET /api/v1/agent-runtimes",
            "POST /api/v1/agent-runtimes/{runtime_id}/desired-state",
            "POST /api/v1/agent-runtimes/{runtime_id}/reconcile",
            "POST /api/v1/agent-runtimes/{runtime_id}/state-sync",
            "DELETE /api/v1/agent-runtimes/{runtime_id}",
            "GET /api/v1/model-routes/{route}", "GET /api/v1/usage",
        ],
        "provider": "proxmox_vm",
        "destroy_policy": "ownership-verified Runtime-Manager-created VMs only (§8)",
    }


# ----------------------------------------------------------------- reconciler
@router.post("/agent-runtimes/{runtime_id}/reconcile")
def reconcile_runtime(runtime_id: str, ident: ServiceIdentity = Depends(_auth("runtime:write"))):
    """Execute desired state for one runtime NOW (§10B). Safe: ownership-verified,
    bounded retries, audited; never destroys."""
    from server_manager.agent_runtime_manager.services.reconciliation import ReconciliationService

    Session = get_session_factory("arm")
    svc = ReconciliationService()
    with Session() as session:
        res = svc.reconcile_runtime(session, uuid.UUID(runtime_id))
        session.commit()
        return ReconcileOut(
            runtime_id=res.runtime_id, desired=res.desired, before=res.before,
            after=res.after, action=res.action, error=res.error, detail=res.detail,
        ).model_dump()


@router.post("/reconcile")
def reconcile_all(ident: ServiceIdentity = Depends(_auth("runtime:write"))):
    """One reconcile pass over all ARM-owned runtimes (§10B). Single-replica by
    deployment discipline (single uvicorn worker); concurrent callers are
    serialized by row-level commits."""
    from server_manager.agent_runtime_manager.services.reconciliation import ReconciliationService

    Session = get_session_factory("arm")
    svc = ReconciliationService()
    with Session() as session:
        results = svc.reconcile_all(session)
        session.commit()
        return {"results": [ReconcileOut(
            runtime_id=x.runtime_id, desired=x.desired, before=x.before,
            after=x.after, action=x.action, error=x.error, detail=x.detail,
        ).model_dump() for x in results], "count": len(results)}


# ----------------------------------------------------------------- state sync
@router.post("/agent-runtimes/{runtime_id}/state-sync")
def report_state_sync(runtime_id: str, body: StateSyncReport, ident: ServiceIdentity = Depends(_auth("runtime:write"))):
    """Record Hermes state-backup health (§10A): branch, last commit SHA, health.

    Called by the worker's state-sync path (or its supervisor) after a push.
    Health vocabulary: PENDING | SYNCING | VERIFIED | STALE | FAILED.
    """
    Session = get_session_factory("arm")
    with Session() as session:
        r = session.get(AgentRuntime, uuid.UUID(runtime_id))
        if r is None:
            raise HTTPException(status_code=404, detail="runtime not found")
        if body.runtime_id and body.runtime_id != runtime_id:
            raise HTTPException(status_code=422, detail="runtime_id mismatch")
        r.hermes_state_repo = body.hermes_state_repo or r.hermes_state_repo
        r.hermes_state_branch = body.hermes_state_branch or r.hermes_state_branch
        r.hermes_profile_name = body.hermes_profile_name or r.hermes_profile_name
        r.last_state_commit_sha = body.last_state_commit_sha
        r.state_sync_health = body.state_sync_health or "VERIFIED"
        session.add(RuntimeEvent(actor=ident.service, action="state_sync_reported", runtime_id=r.runtime_id,
                                 detail={"sha": body.last_state_commit_sha, "branch": r.hermes_state_branch,
                                         "health": r.state_sync_health}))
        session.commit()
        return _runtime_out(r)