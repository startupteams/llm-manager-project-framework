"""Reconciler tests (§10B) — desired-state execution with safety invariants.

Covers: no-op when already correct; RUNNING starts once; STOPPED shuts down
once; ownership mismatch fails closed; PVE unavailable records error without
state flip; recovery bounded with backoff; reconcile idempotent on re-run;
destroy never auto-executes; state-sync reporting endpoint.
"""
from __future__ import annotations

import json
import uuid

import pytest

from server_manager.agent_runtime_manager.models.orm import (
    ActualState,
    AgentRuntime,
    RuntimeEvent,
    RuntimeState,
)
from server_manager.agent_runtime_manager.services.reconciliation import (
    ReconcileSettings,
    ReconciliationService,
)


class ReconcileFakeProvider:
    """Records lifecycle calls; PVE state transitions are scriptable.

    marker_runtime_id: set by tests after the runtime row exists so the
    durable ownership marker matches verify_ownership's runtime-id check.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.pve_state = "stopped"          # what PVE currently reports
        self.shutdown_fails_to_stop = False
        self.marker_runtime_id = "unset"
        self.marker_valid = True
        # multi-runtime support: per-vmid marker runtime ids (defaults to marker_runtime_id)
        self.marker_by_vmid: dict[int, str] = {}

    def _marker(self, vmid: int = 4242) -> str:
        if not self.marker_valid:
            return json.dumps({"created_by_agent_runtime_manager": True})  # incomplete
        rid = self.marker_by_vmid.get(vmid, self.marker_runtime_id)
        return json.dumps({
            "server_runtime_id": rid,
            "acms_agent_id": "agent-recon-0001",
            "provisioning_request_id": "req-x",
            "provider": "proxmox_vm", "node": "miam00111", "vmid": vmid,
            "created_by_agent_runtime_manager": True, "created_at": "2026-09-27T00:00:00Z",
            "template_source": "miam00111/qemu/121",
        })

    # lifecycle
    def start(self, node, vmid):
        self.calls.append(("start", node, vmid))
        self.pve_state = "running"

    def shutdown(self, node, vmid, timeout_s=180):
        self.calls.append(("shutdown", node, vmid))
        if not self.shutdown_fails_to_stop:
            self.pve_state = "stopped"

    def stop_force(self, node, vmid):
        self.calls.append(("stop_force", node, vmid))
        self.pve_state = "stopped"

    # reads
    def vm_status(self, node, vmid):
        self.calls.append(("vm_status", node, vmid))
        return {"status": self.pve_state}

    def vm_exists(self, node, vmid):
        return self.pve_state != "absent"

    def vm_config(self, node, vmid):
        self.calls.append(("vm_config", node, vmid))
        return {"description": self._marker(int(vmid))}


def make_settings(**kw) -> ReconcileSettings:
    d = dict(max_recovery_attempts=3, backoff_base_s=0, backoff_cap_s=0,
             bridge_wait_s=1, poll_interval_s=1)
    d.update(kw)
    return ReconcileSettings(**d)


def make_runtime(session, **kw) -> AgentRuntime:
    d = dict(
        acms_agent_id="agent-recon-0001", provisioning_request_id=f"req-{uuid.uuid4().hex[:12]}",
        name="recon-worker", node="miam00111", vmid=4242,
        desired_state=RuntimeState.DESIRED_RUNNING, actual_state=ActualState.STOPPED,
        created_by_agent_runtime_manager=True, template_source="miam00111/qemu/121",
    )
    d.update(kw)
    r = AgentRuntime(**d)
    session.add(r)
    session.commit()
    return r


@pytest.fixture()
def recon(arm_session, monkeypatch):
    """ReconciliationService with the fake provider + zero backoff."""
    fake = ReconcileFakeProvider()
    svc = ReconciliationService(provider=fake, settings=make_settings())
    return svc, fake


def bind_marker(fake: ReconcileFakeProvider, r: AgentRuntime) -> None:
    """Point the fake's ownership marker at this runtime's id (post-commit)."""
    fake.marker_runtime_id = str(r.runtime_id)
    if r.vmid is not None:
        fake.marker_by_vmid[r.vmid] = str(r.runtime_id)


# --------------------------------------------------------------------- tests
def test_noop_when_already_running(arm_session, recon):
    svc, fake = recon
    fake.pve_state = "running"
    r = make_runtime(arm_session, actual_state=ActualState.RUNNING)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "noop"
    assert fake.calls == [("vm_status", "miam00111", 4242)]  # no start call
    assert r.actual_state == ActualState.RUNNING


def test_noop_when_already_stopped_and_desired_stopped(arm_session, recon):
    svc, fake = recon
    r = make_runtime(arm_session, desired_state=RuntimeState.DESIRED_STOPPED)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "noop"
    assert ("shutdown", "miam00111", 4242) not in fake.calls
    assert r.actual_state == ActualState.STOPPED


def test_desired_running_starts_once(arm_session, recon):
    svc, fake = recon
    r = make_runtime(arm_session)  # stopped, desired running
    bind_marker(fake, r)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "started"
    starts = [c for c in fake.calls if c[0] == "start"]
    assert len(starts) == 1
    assert r.actual_state == ActualState.RUNNING
    # re-run: no second start
    fake.calls.clear()
    res2 = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res2.action == "noop"
    assert [c for c in fake.calls if c[0] == "start"] == []


def test_desired_stopped_shuts_down_once(arm_session, recon):
    svc, fake = recon
    fake.pve_state = "running"
    r = make_runtime(arm_session, desired_state=RuntimeState.DESIRED_STOPPED,
                     actual_state=ActualState.RUNNING)
    bind_marker(fake, r)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "shutdown"
    assert len([c for c in fake.calls if c[0] == "shutdown"]) == 1
    assert r.actual_state == ActualState.STOPPED
    # never destroys on reconcile
    assert not any(c[0] == "destroy" for c in fake.calls)


def test_ownership_mismatch_fails_closed(arm_session, recon):
    svc, fake = recon
    fake.marker_valid = False  # incomplete marker
    r = make_runtime(arm_session)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "error"
    assert "ownership" in (res.error or "")
    assert [c for c in fake.calls if c[0] == "start"] == []  # never touched
    # audit recorded
    ev = arm_session.query(RuntimeEvent).filter_by(action="reconcile_refused").all()
    assert len(ev) == 1


def test_unowned_runtime_refused(arm_session, recon):
    svc, fake = recon
    r = make_runtime(arm_session, created_by_agent_runtime_manager=False)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "error"
    assert "ownership" in (res.error or "")


def test_pve_unavailable_records_error_no_state_flip(arm_session, monkeypatch):
    class DeadProvider(ReconcileFakeProvider):
        def vm_status(self, node, vmid):
            raise RuntimeError("connection refused")

    fake = DeadProvider()
    svc = ReconciliationService(provider=fake, settings=make_settings())
    r = make_runtime(arm_session)
    before = r.actual_state
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "error"
    assert "PVE unavailable" in res.error
    assert r.actual_state == before  # unchanged
    assert "PVE unavailable" in (r.last_error or "")


def test_recovery_bounded_with_attempts(arm_session, recon):
    svc, fake = recon
    r = make_runtime(arm_session, recovery_count=3)  # already exhausted
    bind_marker(fake, r)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "error"
    assert "recovery exhausted" in res.error
    assert [c for c in fake.calls if c[0] == "start"] == []


def test_slow_bridge_recovery_backoff_then_start(arm_session):
    fake = ReconcileFakeProvider()
    settings = make_settings(backoff_base_s=0, max_recovery_attempts=3)
    svc = ReconciliationService(provider=fake, settings=settings)
    r = make_runtime(arm_session, recovery_count=1)
    bind_marker(fake, r)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    # attempt 2 of 3 proceeds (after a 0s backoff in tests)
    assert res.action == "started"
    assert r.recovery_count == 2


def test_reconciler_never_auto_destroys(arm_session, recon):
    svc, fake = recon
    r = make_runtime(arm_session, desired_state=RuntimeState.DESIRED_DESTROYED,
                     actual_state=ActualState.RUNNING)
    fake.pve_state = "running"
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "noop"
    assert fake.calls and all(c[0] in ("vm_status",) for c in fake.calls)


def test_shutdown_nonconvergence_recorded(arm_session, recon):
    svc, fake = recon
    fake.pve_state = "running"
    fake.shutdown_fails_to_stop = True
    r = make_runtime(arm_session, desired_state=RuntimeState.DESIRED_STOPPED,
                     actual_state=ActualState.RUNNING)
    bind_marker(fake, r)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "error"
    assert "did not converge" in res.error
    assert r.actual_state == ActualState.RUNNING  # honest: not verified stopped


def test_observe_corrects_stale_actual(arm_session, recon):
    svc, fake = recon
    # DB thinks STOPPED but PVE says running → corrected to RUNNING, no action
    r = make_runtime(arm_session, actual_state=ActualState.STOPPED)
    fake.pve_state = "running"
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "noop"
    assert r.actual_state == ActualState.RUNNING


def test_reconcile_all_pass(arm_session, recon):
    svc, fake = recon
    fake.pve_state = "stopped"
    r1 = make_runtime(arm_session, vmid=4242)
    r2 = make_runtime(arm_session, desired_state=RuntimeState.DESIRED_STOPPED, vmid=4243)
    bind_marker(fake, r1)
    bind_marker(fake, r2)
    results = svc.reconcile_all(arm_session)
    actions = {x.runtime_id: x.action for x in results}
    assert actions[str(r1.runtime_id)] == "started"
    # r1's start flipped the shared fake PVE state to running; r2 (desired STOPPED) must then shut it down.
    assert actions[str(r2.runtime_id)] == "shutdown"
    assert r1.last_reconcile_at is not None
    assert r2.last_reconcile_at is not None


# ----------------------------------------------------------------- API layer
def test_api_reconcile_endpoint(arm_client, arm_session, recon, auth_headers, add_token, monkeypatch):
    add_token("svc-recon-test", "tok-recon-1", "ALL")
    svc, fake = recon
    # The API endpoint builds its own ReconciliationService() → inject the fake provider.
    from server_manager.agent_runtime_manager.services import reconciliation as recon_mod

    def _fake_init(self, provider=None, settings=None):
        self.provider = fake
        self.settings = make_settings()
    monkeypatch.setattr(recon_mod.ReconciliationService, "__init__", _fake_init)
    r = make_runtime(arm_session)
    bind_marker(fake, r)
    resp = arm_client.post(f"/api/v1/agent-runtimes/{r.runtime_id}/reconcile",
                           headers=auth_headers("tok-recon-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "started"
    assert body["after"] == "RUNNING"


def test_api_reconcile_requires_scope(arm_client, add_token, auth_headers):
    add_token("svc-readonly-r", "tok-ro-recon", "runtime:read,job:read,health:read")
    resp = arm_client.post("/api/v1/agent-runtimes/00000000-0000-0000-0000-000000000000/reconcile",
                           headers=auth_headers("tok-ro-recon"))
    assert resp.status_code == 403


def test_api_state_sync_report(arm_client, arm_session, auth_headers, add_token):
    add_token("svc-sync-test", "tok-sync-1", "ALL")
    r = make_runtime(arm_session)
    resp = arm_client.post(f"/api/v1/agent-runtimes/{r.runtime_id}/state-sync",
                           headers=auth_headers("tok-sync-1"),
                           json={
                               "runtime_id": str(r.runtime_id),
                               "hermes_state_repo": "startupteams/hermes-base-setup",
                               "hermes_state_branch": "agent/22ac1b19",
                               "hermes_profile_name": "acms-worker-001",
                               "last_state_commit_sha": "180dd563c746fa88ebb55844ff25a9fe1f9d8c26",
                               "state_sync_health": "VERIFIED",
                               "reported_by": "hermes-state-sync",
                               "observed_at": "2026-09-28T01:10:21Z",
                           })
    assert resp.status_code == 200
    body = resp.json()
    assert body["state_sync_health"] == "VERIFIED"
    assert body["last_state_commit_sha"].startswith("180dd563")
    assert body["hermes_state_branch"] == "agent/22ac1b19"
    # audit recorded
    ev = arm_session.query(RuntimeEvent).filter_by(action="state_sync_reported").all()
    assert len(ev) == 1
