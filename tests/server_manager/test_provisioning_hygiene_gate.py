"""Phase D: clone-hygiene wired into the live provisioner (fail-closed gate).

Proves the VM108/VM124 `.203` impostor class is rejected BEFORE a runtime
reaches READY / bridge registration: a template clone carrying an inherited
static netplan identity (or an IP already owned by another runtime) fails the
job in HYGIENE_GATE — never marked RUNNING, never registered as a bridge.
"""
from __future__ import annotations

import json

import pytest

from server_manager.agent_runtime_manager.models.orm import (
    ActualState,
    JobState,
    RuntimeEvent,
)
from server_manager.agent_runtime_manager.providers.proxmox_vm import ProxmoxVMProvider
from server_manager.agent_runtime_manager.services.provisioning import (
    ProvisionRequest,
    ProvisioningService,
)


class ImpostorVMProvider:
    """Simulates the VM108 trap: the clone boots and DHCP-hands out an IP, but
    the guest still holds the TEMPLATE's static netplan identity (10.0.20.203)
    while the intended new IP is 10.0.20.210."""

    def __init__(self, inherited_static=("10.0.20.203",), ip="10.0.20.210"):
        self.ip = ip
        self.inherited_static = list(inherited_static)
        self.agent_id = "agent-new-001"

    def list_nodes(self):
        # placement policy vantage: one healthy generic node available
        return [("miam-00135", "online")]

    def next_vmid(self):
        return 300

    def template_storage(self, spec):
        return getattr(self, "_fake_template_storage", "testthin")

    def storage_active(self, node, storage):
        return True

    def clone_template(self, spec, new_vmid):
        pass

    def wait_clone_lock_release(self, node, vmid):
        pass

    def configure_cloud_init(self, spec, vmid, extra=None):
        pass

    def start(self, node, vmid):
        pass

    def wait_for_ip(self, node, vmid, max_s=300):
        return self.ip

    def vm_exists(self, node, vmid):
        return True

    def vm_config(self, node, vmid):
        return {
            "description": json.dumps({
                "created_by_agent_runtime_manager": True,
                "acms_agent_id": self.agent_id,
            }),
            "net0": "virtio,bridge=vmbr0,mac=BC:24:11:AA:BB:CD",
        }


def _svc(monkeypatch, fake):
    svc = ProvisioningService()
    monkeypatch.setattr(svc, "provider", fake)
    # the hygiene gate reads the guest marker's acms_agent_id — the fake
    # provider serves whatever the submitted request declares (identity
    # matches by design; mismatches are covered by dedicated tests).
    real_submit = svc.submit

    def _submit(session, req, requested_by, authority):
        fake.agent_id = req.acms_agent_id
        return real_submit(session, req, requested_by, authority)

    svc.submit = _submit
    return svc


def test_inherited_netplan_rejected_before_ready(arm_session, monkeypatch):
    """THE .203 impostor class: template static netplan survives the clone →
    job FAILED in HYGIENE_GATE; runtime never RUNNING; bridge never registered;
    durable provisioning failure recorded."""
    fake = ImpostorVMProvider()  # guest still holds the template's static .203
    svc = _svc(monkeypatch, fake)

    # the hygiene module's netplan probe must see the inherited static — patch
    # the qga-backed helper to report the template-carried identity.
    import server_manager.agent_runtime_manager.services.clone_hygiene as ch

    monkeypatch.setattr(ch, "_guest_netplan_static_ips", lambda node, vmid: fake.inherited_static)

    req = ProvisionRequest(acms_agent_id="agent-new-001", request_id="req-impostor-1",
                           name="worker-impostor")
    job, _ = svc.submit(arm_session, req, "svc-acms", "test")
    svc.run_job(arm_session, job.job_id)

    arm_session.refresh(job)
    arm_session.refresh(job.runtime)
    assert job.state == JobState.FAILED
    assert "clone hygiene gate rejected" in (job.error or "")
    # FAIL CLOSED: never READY, never bridge-registered
    assert job.runtime.actual_state == ActualState.ERROR
    assert job.runtime.vmid is None  # never promoted to operational identity
    # durable failure audit — carries the VMID for human disposal
    events = arm_session.query(RuntimeEvent).filter_by(runtime_id=job.runtime.runtime_id).all()
    actions = [e.action for e in events]
    assert "clone_hygiene_failed" in actions
    hygiene_ev = next(e for e in events if e.action == "clone_hygiene_failed")
    assert hygiene_ev.detail.get("vmid") == 300
    # ownership metadata carries the hygiene verdict (durable, not just a log)
    assert job.runtime.ownership_meta["clone_hygiene"]["ok"] is False


def test_conflicting_ip_owned_by_other_runtime_rejected(arm_session, monkeypatch):
    """Candidate IP already bound to another runtime's metadata → rejected."""
    import server_manager.agent_runtime_manager.models.orm as orm

    fake = ImpostorVMProvider(ip="10.0.20.203", inherited_static=[])  # clean netplan, but .203 is taken
    svc = _svc(monkeypatch, fake)

    # another runtime already owns .203
    other = orm.AgentRuntime(
        acms_agent_id="agent-other", name="worker-other",
        provisioning_request_id="req-other-1",
        desired_state=orm.RuntimeState.DESIRED_RUNNING,
        actual_state=orm.ActualState.RUNNING,
    )
    arm_session.add(other)
    arm_session.flush()
    other.ownership_meta = {"ip": "10.0.20.203"}
    arm_session.commit()

    import server_manager.agent_runtime_manager.services.clone_hygiene as ch
    monkeypatch.setattr(ch, "_guest_netplan_static_ips", lambda node, vmid: [])

    req = ProvisionRequest(acms_agent_id="agent-new-002", request_id="req-impostor-2",
                           name="worker-collide")
    job, _ = svc.submit(arm_session, req, "svc-acms", "test")
    svc.run_job(arm_session, job.job_id)

    arm_session.refresh(job)
    assert job.state == JobState.FAILED
    assert "ip_not_owned_by_other_runtime" in (job.error or "")
    assert job.runtime.actual_state == ActualState.ERROR


def test_clean_clone_passes_gate_and_reaches_ready(arm_session, monkeypatch):
    """Happy path survives the gate: re-identified guest → DONE."""
    fake = ImpostorVMProvider(ip="10.0.20.210", inherited_static=[])
    svc = _svc(monkeypatch, fake)

    import server_manager.agent_runtime_manager.services.clone_hygiene as ch
    monkeypatch.setattr(ch, "_guest_netplan_static_ips", lambda node, vmid: ["10.0.20.210"])

    req = ProvisionRequest(acms_agent_id="agent-new-003", request_id="req-clean-1",
                           name="worker-clean")
    job, _ = svc.submit(arm_session, req, "svc-acms", "test")
    svc.run_job(arm_session, job.job_id)

    arm_session.refresh(job)
    assert job.state == JobState.DONE, job.error
    arm_session.refresh(job.runtime)
    assert job.runtime.actual_state == ActualState.RUNNING
    assert job.runtime.ownership_meta["clone_hygiene"]["ok"] is True