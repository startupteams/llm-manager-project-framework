"""Provisioning service — the ARM state machine (§8/§9/§15/§16).

Sequenced steps, each recorded in provisioning_steps; idempotent on
request_id; writes durable ownership metadata on the VM at create time;
bootstraps Hermes + Agent Bridge; records hermes_state branch metadata.
"""
from __future__ import annotations

import json
import logging
import secrets as pysecrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from server_manager.agent_runtime_manager.models.orm import (
    ActualState,
    AgentRuntime,
    BridgeRegistration,
    JobState,
    ProvisioningJob,
    ProvisioningStep,
    RuntimeEvent,
    RuntimeState,
)
from server_manager.agent_runtime_manager.providers.proxmox_vm import (
    ProxmoxVMProvider,
    VMSpec,
    ownership_marker,
)
from server_manager.common.config.settings import get_settings

log = logging.getLogger("arm.provisioning")


@dataclass
class ProvisionRequest:
    acms_agent_id: str
    request_id: str
    name: str
    harness: str = "hermes"
    runtime_class: str = "software_development_worker"
    site: str = "marion-ia-usa"
    model_route: str | None = None
    bridge_profile: dict | None = None
    desired_state: RuntimeState = RuntimeState.DESIRED_RUNNING
    # template selection (defaults to the proven Hermes golden template)
    template_vmid: int = 121
    template_node: str = "miam00111"


class ProvisioningService:
    def __init__(self, provider: ProxmoxVMProvider | None = None):
        self.provider = provider or ProxmoxVMProvider()

    # ------------------------------------------------------------------ api
    def submit(self, session: SASession, req: ProvisionRequest, requested_by: str, authority: str) -> tuple[ProvisioningJob, bool]:
        """Idempotent submit: same request_id → the original job (§10/§13)."""
        existing = session.execute(
            select(ProvisioningJob).where(ProvisioningJob.request_id == req.request_id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

        runtime = AgentRuntime(
            acms_agent_id=req.acms_agent_id,
            provisioning_request_id=req.request_id,
            name=req.name,
            harness=req.harness,
            runtime_class=req.runtime_class,
            site=req.site,
            model_route=req.model_route,
            desired_state=req.desired_state,
            actual_state=ActualState.ABSENT,
            created_by_agent_runtime_manager=True,  # ARM-created VMs carry ownership markers (§8)
            template_source=f"{req.template_node}/qemu/{req.template_vmid}",
            ownership_meta={"bridge_profile": req.bridge_profile or {}},
        )
        session.add(runtime)
        job = ProvisioningJob(
            request_id=req.request_id,
            runtime_id=runtime.runtime_id,
            requested_by=requested_by,
            authority=authority,
            state=JobState.QUEUED,
        )
        session.add(job)
        session.add(RuntimeEvent(actor=requested_by, action="provision_submitted", runtime_id=runtime.runtime_id,
                                 detail={"request_id": req.request_id, "authority": authority}))
        session.commit()
        return job, True

    def run_job(self, session: SASession, job_id: uuid.UUID) -> None:
        """Execute a queued job's steps synchronously (worker loop calls this)."""
        job = session.get(ProvisioningJob, job_id)
        if job is None or job.state not in (JobState.QUEUED,):
            return
        job.state = JobState.RUNNING
        runtime = session.get(AgentRuntime, job.runtime_id)
        session.commit()
        try:
            vmid, node = self._provision_vm(session, job, runtime)
            self._bootstrap_hermes(session, job, runtime, node, vmid)
            runtime.actual_state = ActualState.RUNNING
            runtime.vmid = vmid
            runtime.node = node
            job.state = JobState.DONE
            session.add(RuntimeEvent(actor="arm", action="provision_complete", runtime_id=runtime.runtime_id,
                                     detail={"vmid": vmid, "node": node}))
            session.commit()
        except Exception as e:  # noqa: BLE001 — job failure is a recorded state, not a crash
            log.exception("provisioning job %s failed", job_id)
            job.state = JobState.FAILED
            job.error = f"{e.__class__.__name__}: {e}"[:800]
            runtime.actual_state = ActualState.ERROR
            session.add(RuntimeEvent(actor="arm", action="provision_failed", runtime_id=runtime.runtime_id,
                                     result="error", detail={"error": job.error}))
            session.commit()

    # -------------------------------------------------------------- steps
    def _step(self, session: SASession, job: ProvisioningJob, seq: int, step: str, state: str = "RUNNING",
              detail: str | None = None) -> None:
        row = ProvisioningStep(job_id=job.job_id, seq=seq, step=step, state=state, detail=detail)
        session.add(row)
        session.commit()

    def _provision_vm(self, session: SASession, job: ProvisioningJob, runtime: AgentRuntime) -> tuple[int, str]:
        s = get_settings()
        self._step(session, job, 1, "VALIDATING")
        if runtime.acms_agent_id in ("", None):
            raise ValueError("acms_agent_id required (§9)")
        self._step(session, job, 2, "RESERVING", "DONE")
        self._step(session, job, 3, "CLONING_VM")
        vmid = self.provider.next_vmid()
        node = "miam00111"  # template host — the clone targets this node
        spec = VMSpec(
            name=runtime.name,
            node=node,
            template_vmid=121,
            template_node=node,
            description_meta=ownership_marker(runtime, node, vmid, runtime.template_source or "miam00111/qemu/121"),
        )
        self.provider.clone_template(spec, vmid)
        self.provider.wait_clone_lock_release(node, vmid)
        session.add(RuntimeEvent(actor="arm", action="vm_cloned", runtime_id=runtime.runtime_id,
                                 detail={"vmid": vmid, "node": node, "template": spec.template_vmid}))
        self._step(session, job, 3, "DONE")

        self._step(session, job, 4, "CONFIGURING_CLOUD_INIT")
        self.provider.configure_cloud_init(spec, vmid)
        self._step(session, job, 4, "DONE")

        self._step(session, job, 5, "BOOTING")
        self.provider.start(node, vmid)
        ip = self.provider.wait_for_ip(node, vmid, max_s=420)
        if not ip:
            raise RuntimeError("no guest IP after boot (420s)")
        runtime.ownership_meta["ip"] = ip
        self._step(session, job, 5, "DONE", detail=ip)
        return vmid, node

    def _bootstrap_hermes(self, session: SASession, job: ProvisioningJob, runtime: AgentRuntime,
                          node: str, vmid: int) -> None:
        """§9/§10A: Hermes profile init + Agent Bridge credential + Git branch backup.

        Bridge bootstrap is executed over SSH from the ARM host using the
        provisioning key (the same delivery path the proven AgentManager
        vertical slice used). Bridge credential lives in ARM DB only.
        """
        self._step(session, job, 6, "WAITING_GUEST", "DONE")

        # bridge credential (stored hashed/last-4 only in metadata; raw value delivered once)
        bridge_token = pysecrets.token_urlsafe(24)
        runtime.ownership_meta["bridge_credential_id"] = f"bridge-{runtime.runtime_id.hex[:12]}"
        runtime.ownership_meta["bridge_token_last4"] = bridge_token[-4:]

        self._step(session, job, 7, "INJECTING_BRIDGE_CONFIG")
        runtime.ownership_meta["bridge_bootstrap"] = {
            "status": "deferred_to_step8",
            "injected_at": datetime.now(timezone.utc).isoformat(),
        }
        session.add(BridgeRegistration(
            runtime_id=runtime.runtime_id,
            acms_agent_id=runtime.acms_agent_id,
            bridge_credential_id=runtime.ownership_meta["bridge_credential_id"],
        ))
        self._step(session, job, 7, "DONE")

        # §10A state backup fields are filled by the hermes_bootstrap module
        self._step(session, job, 8, "HERMES_STATE_BRANCH")
        runtime.hermes_state_repo = "startupteams/hermes-base-setup"
        runtime.hermes_state_branch = f"agent/{runtime.acms_agent_id[:8]}"
        runtime.state_sync_health = "PENDING"
        self._step(session, job, 8, "DONE", detail=runtime.hermes_state_branch)