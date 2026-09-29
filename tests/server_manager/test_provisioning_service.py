"""Provisioning service tests — idempotency, state machine, failure path."""
from __future__ import annotations

import pytest

from server_manager.agent_runtime_manager.models.orm import (
    ActualState,
    JobState,
)
from server_manager.agent_runtime_manager.providers.proxmox_vm import ProxmoxVMProvider
from server_manager.agent_runtime_manager.services.provisioning import (
    ProvisionRequest,
    ProvisioningService,
)


class FakeVMProvider:
    def __init__(self, fail_ip=False):
        self.fail_ip = fail_ip
        self.clone_calls = []
        self.destroyed = []

    def next_vmid(self):
        return 200

    def clone_template(self, spec, new_vmid):
        self.clone_calls.append((spec.name, new_vmid))

    def wait_clone_lock_release(self, node, vmid):
        pass

    def configure_cloud_init(self, spec, vmid, extra=None):
        pass

    def start(self, node, vmid):
        pass

    def wait_for_ip(self, node, vmid, max_s=300):
        return None if self.fail_ip else "10.0.20.200"

    def vm_exists(self, node, vmid):
        return True

    def vm_config(self, node, vmid):
        import json
        # marker carries acms_agent_id — the hygiene gate reads guest identity
        # from this durable clone-time marker (ADR wiring, Phase D)
        return {"description": json.dumps({
            "created_by_agent_runtime_manager": True,
            "acms_agent_id": getattr(self, "agent_id", None),
        }),
        "net0": "virtio,bridge=vmbr0,mac=BC:24:11:AA:BB:CC"}


def _svc(monkeypatch, fake):
    svc = ProvisioningService()
    monkeypatch.setattr(svc, "provider", fake)
    # hygiene gate reads the guest marker's acms_agent_id — FakeVMProvider
    # serves whatever the test's requests declare (identity matches by design).
    real_submit = svc.submit

    def _submit(session, req, requested_by, authority):
        fake.agent_id = req.acms_agent_id
        return real_submit(session, req, requested_by, authority)

    svc.submit = _submit
    return svc


def test_submit_idempotent(arm_session, monkeypatch):
    svc = _svc(monkeypatch, FakeVMProvider())
    req = ProvisionRequest(acms_agent_id="agent-idempot-1", request_id="req-idem-1", name="w1")
    job1, created1 = svc.submit(arm_session, req, "svc-acms", "test")
    assert created1 is True
    job2, created2 = svc.submit(arm_session, req, "svc-acms", "test")
    assert created2 is False
    assert job2.job_id == job1.job_id


def test_run_job_happy_path(arm_session, monkeypatch):
    svc = _svc(monkeypatch, FakeVMProvider())
    req = ProvisionRequest(acms_agent_id="agent-happy-001", request_id="req-happy-1", name="worker-a")
    job, _ = svc.submit(arm_session, req, "svc-acms", "test")
    svc.run_job(arm_session, job.job_id)
    arm_session.refresh(job)
    assert job.state == JobState.DONE
    rt = job.runtime
    arm_session.refresh(rt)
    assert rt.actual_state == ActualState.RUNNING
    assert rt.vmid == 200
    assert rt.ownership_meta["ip"] == "10.0.20.200"
    # steps recorded: seq 1..8 present
    seqs = sorted(s.seq for s in job.steps)
    assert seqs == [1, 2, 3, 4, 5, 6, 7, 8]


def test_run_job_ip_timeout_failure(arm_session, monkeypatch):
    svc = _svc(monkeypatch, FakeVMProvider(fail_ip=True))
    req = ProvisionRequest(acms_agent_id="agent-fail-0001", request_id="req-fail-1", name="worker-b")
    job, _ = svc.submit(arm_session, req, "svc-acms", "test")
    svc.run_job(arm_session, job.job_id)
    arm_session.refresh(job)
    assert job.state == JobState.FAILED
    assert "no guest IP" in job.error
    rt = job.runtime
    arm_session.refresh(rt)
    assert rt.actual_state == ActualState.ERROR
    # audit event recorded
    from sqlalchemy import select

    from server_manager.agent_runtime_manager.models.orm import RuntimeEvent

    ev = arm_session.execute(
        select(RuntimeEvent).where(RuntimeEvent.action == "provision_failed")
    ).scalars().all()
    assert len(ev) == 1
