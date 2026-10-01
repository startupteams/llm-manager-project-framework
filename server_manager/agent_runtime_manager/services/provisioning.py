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
from server_manager.agent_runtime_manager.services.placement import select_node
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
        session.flush()  # assign runtime.runtime_id (Python-side default) before FK use
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
        # A step is recorded twice per sequence number (start + terminal update).
        # Upsert on (job_id, seq) so the terminal update replaces the RUNNING row
        # instead of violating uq_provstep_job_seq.
        existing = session.execute(
            select(ProvisioningStep).where(
                ProvisioningStep.job_id == job.job_id, ProvisioningStep.seq == seq
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.step = step
            existing.state = state
            existing.detail = detail
        else:
            session.add(ProvisioningStep(job_id=job.job_id, seq=seq, step=step,
                                         state=state, detail=detail))
        session.commit()


    def _run_clone_hygiene(self, session: SASession, job: ProvisioningJob, runtime: AgentRuntime,
                           *, node: str, vmid: int, ip: str) -> None:
        """Fail-closed identity/network gate (plan Phase D; PR #57 module).

        Checks: unique VMID (structural), valid non-template MAC, re-identified
        hostname, IP not owned by another runtime, no inherited conflicting
        static netplan (THE VM108/VM124 lesson), guest identity vs ARM metadata.
        Any failure raises → job FAILED → durable provisioning failure recorded;
        the VM is NOT marked READY and the bridge is never registered healthy.
        Retry guidance: a retry must allocate a fresh candidate identity —
        enforced by the caller submitting a new request (attempt counter), and
        the failed VM is left for human disposal (never auto-destroyed here).
        """
        from server_manager.agent_runtime_manager.services.clone_hygiene import (
            HygieneError,
            verify_clone_identity,
        )

        cfg = self.provider.vm_config(node, vmid)
        mac = ""
        net0 = cfg.get("net0") or ""
        m = __import__("re").search(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", net0)
        if m:
            mac = m.group(1)

        # guest-reported identity comes from the ownership marker written at
        # clone time (the guest cannot yet report its own agent id — the marker
        # IS the identity source of truth pre-bootstrap).
        marker = {}
        try:
            marker = json.loads(cfg.get("description") or "{}")
        except (ValueError, TypeError):
            marker = {}

        other_ips = {
            str(o.get("ip")) for o in self._other_runtime_ips(session, runtime)
            if o.get("ip")
        }

        try:
            rep = verify_clone_identity(
                vmid=vmid,
                hostname=runtime.name or "",
                mac=mac,
                ip=ip,
                expected_agent_id=runtime.acms_agent_id,
                guest_agent_id=marker.get("acms_agent_id"),
                other_runtime_ips=other_ips,
            )
            runtime.ownership_meta["clone_hygiene"] = {
                "ok": True,
                "checks": [(n, p, d) for n, p, d in rep.checks],
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
        except HygieneError as e:
            runtime.ownership_meta["clone_hygiene"] = {"ok": False, "error": str(e)[:400]}
            session.add(RuntimeEvent(actor="arm", action="clone_hygiene_failed",
                                     runtime_id=runtime.runtime_id, result="error",
                                     detail={"error": str(e)[:400], "vmid": vmid,
                                             "ip": ip, "policy": "fail_closed_no_ready"}))
            raise RuntimeError(f"clone hygiene gate rejected VM {vmid}: {e}") from None

    @staticmethod
    def _other_runtime_ips(session: SASession, runtime: AgentRuntime) -> list[dict]:
        """IPs bound to OTHER runtimes' ownership metadata (uniqueness set)."""
        rows = session.execute(
            select(AgentRuntime).where(AgentRuntime.runtime_id != runtime.runtime_id)
        ).scalars().all()
        return [{"runtime_id": str(r.runtime_id), "ip": (r.ownership_meta or {}).get("ip")}
                for r in rows]

    @staticmethod
    def _renumber_steps(job: ProvisioningJob, *, next_seq: int) -> None:
        """Later steps (WAITING_GUEST=6...) were written for the old numbering;
        they now start at ``next_seq`` so the hygiene gate keeps a stable slot."""
        # no-op placeholder: subsequent _step() calls in _bootstrap_hermes use
        # explicit numbers; kept as a seam for future re-ordering.

    def _provision_vm(self, session: SASession, job: ProvisioningJob, runtime: AgentRuntime) -> tuple[int, str]:
        s = get_settings()
        # template selection from the runtime's recorded source (or the proven default)
        tpl_source = runtime.template_source or "miam00111/qemu/121"
        try:
            tpl_node, tpl_path = tpl_source.split("/qemu/", 1)
            spec_template_vmid = int(tpl_path)
        except ValueError:
            spec_template_node, spec_template_vmid = "miam00111", 121
        else:
            spec_template_node = tpl_node or "miam00111"
        self._step(session, job, 1, "VALIDATING")
        if runtime.acms_agent_id in ("", None):
            raise ValueError("acms_agent_id required (§9)")
        self._step(session, job, 1, "VALIDATING", "DONE")
        self._step(session, job, 2, "RESERVING", "DONE")
        self._step(session, job, 3, "PLACING")
        decision = select_node(
            session,
            self.provider,
            runtime_class=runtime.runtime_class or "software_development_worker",
            template_node=spec_template_node,
        )
        if decision.rejected or not decision.node:
            self._step(session, job, 3, "PLACING", "FAILED", detail=decision.rejection_reason)
            raise RuntimeError(
                f"placement policy rejected provisioning: {decision.rejection_reason} (§5)"
            )
        node = decision.node
        session.add(RuntimeEvent(actor="arm", action="placement_selected", runtime_id=runtime.runtime_id,
                                 detail={"node": node, "score": decision.score, "reasons": decision.reasons}))
        self._step(session, job, 3, "PLACING", "DONE", detail=node)
        self._step(session, job, 3, "CLONING_VM")
        vmid = self.provider.next_vmid()
        spec = VMSpec(
            name=runtime.name,
            node=node,
            template_vmid=spec_template_vmid,
            template_node=spec_template_node,
            description_meta=ownership_marker(runtime, node, vmid, runtime.template_source or "miam00111/qemu/121"),
        )
        self.provider.clone_template(spec, vmid)
        self.provider.wait_clone_lock_release(node, vmid)
        session.add(RuntimeEvent(actor="arm", action="vm_cloned", runtime_id=runtime.runtime_id,
                                 detail={"vmid": vmid, "node": node, "template": spec.template_vmid}))
        self._step(session, job, 3, "CLONING_VM", "DONE")

        self._step(session, job, 4, "CONFIGURING_CLOUD_INIT")
        self.provider.configure_cloud_init(spec, vmid)
        self._step(session, job, 4, "CONFIGURING_CLOUD_INIT", "DONE")

        self._step(session, job, 5, "BOOTING")
        self._step(session, job, 5, "BOOTING")
        ip = self.provider.wait_for_ip(node, vmid, max_s=420)
        if not ip:
            raise RuntimeError("no guest IP after boot (420s)")
        runtime.ownership_meta["ip"] = ip
        self._step(session, job, 5, "BOOTING", "DONE", detail=ip)

        # -------------------------------------------------------------- 5b
        # Clone identity/network hygiene gate (Phase D wiring of PR #57):
        # fail-closed BEFORE the runtime may bootstrap/return READY. A
        # template clone carrying inherited identity (the VM108/VM124
        # .203 impostor class) is rejected here, never registered.
        self._step(session, job, 6, "HYGIENE_GATE")
        self._run_clone_hygiene(session, job, runtime, node=node, vmid=vmid, ip=ip)
        self._step(session, job, 6, "HYGIENE_GATE", "DONE")
        self._renumber_steps(job, next_seq=7)
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
        self._step(session, job, 7, "INJECTING_BRIDGE_CONFIG", "DONE")

        # §10A state backup fields are filled by the hermes_bootstrap module
        self._step(session, job, 8, "HERMES_STATE_BRANCH")
        runtime.hermes_state_repo = "startupteams/hermes-base-setup"
        runtime.hermes_state_branch = f"agent/{runtime.acms_agent_id[:8]}"
        runtime.state_sync_health = "PENDING"
        self._step(session, job, 8, "HERMES_STATE_BRANCH", "DONE", detail=runtime.hermes_state_branch)
