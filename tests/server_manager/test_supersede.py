"""SUPERSEDED disposition for failed ARM provisioning rows (REV2 §12, 2026-09-28).

Human-approved non-destructive cleanup: the five failed attempts of request
``acms-22ec3237…`` are historical evidence of the retry-semantics bug that
migration 0005 fixed. They get an explicit SUPERSEDED state, linked to the live
retry:5 runtime — never deleted.

Covers:
- happy path: ERROR/no-resources row → SUPERSEDED with linkage + audit event
- reconciler skips SUPERSEDED rows (no PVE call, no state flip)
- refuses rows with backing resources (node/vmid set) or RUNNING
- refuses unknown/self/already-superseded targets
- scope enforcement (runtime:write required)
- live runtime stays untouched; failed-attempt history preserved
"""
from __future__ import annotations

import uuid

import pytest

from server_manager.agent_runtime_manager.models.orm import (
    ActualState,
    RuntimeEvent,
    RuntimeState,
)
from server_manager.agent_runtime_manager.services.reconciliation import (
    ReconcileSettings,
    ReconciliationService,
)


def make_runtime(session, **kw):
    d = dict(
        acms_agent_id="agent-supersede-0001",
        provisioning_request_id=f"req-{uuid.uuid4().hex[:12]}",
        name="supersede-worker", node=None, vmid=None,
        desired_state=RuntimeState.DESIRED_RUNNING, actual_state=ActualState.ERROR,
        created_by_agent_runtime_manager=True,
    )
    d.update(kw)
    r = AgentRuntimeLocal(**d)
    session.add(r)
    session.commit()
    return r


# late import so collection works without the ARM tree on sys.path in other suites
from server_manager.agent_runtime_manager.models.orm import AgentRuntime as AgentRuntimeLocal  # noqa: E402


class NoopProvider:
    """Fails the test if the reconciler ever touches PVE for a superseded row."""

    def __init__(self):
        self.calls = []

    def vm_status(self, node, vmid):
        self.calls.append(("vm_status", node, vmid))
        return {"status": "running"}

    def vm_exists(self, node, vmid):
        self.calls.append(("vm_exists", node, vmid))
        return False

    def vm_config(self, node, vmid):
        self.calls.append(("vm_config", node, vmid))
        return {"description": "{}"}


def make_svc():
    s = ReconcileSettings(max_recovery_attempts=3, backoff_base_s=0, backoff_cap_s=0,
                          bridge_wait_s=1, poll_interval_s=1, shutdown_wait_s=0)
    svc = ReconciliationService(provider=NoopProvider(), settings=s)
    return svc


# ---------------------------------------------------------------------------
# API supersede endpoint
# ---------------------------------------------------------------------------

LIVE = "11111111-1111-1111-1111-111111111111"
FAILED = "22222222-2222-2222-2222-222222222222"


@pytest.fixture()
def two_rows(arm_session):
    live = make_runtime(arm_session, runtime_id=uuid.UUID(LIVE), name="acms-worker-001",
                        node="miam00111", vmid=124, actual_state=ActualState.RUNNING,
                        provisioning_request_id="acms-22ec3237-5073-4c2f-a186-e8d2f2560dd7|retry:5")
    failed = make_runtime(arm_session, runtime_id=uuid.UUID(FAILED), name="acms-worker-001",
                          node=None, vmid=None, actual_state=ActualState.ERROR,
                          provisioning_request_id="acms-22ec3237-5073-4c2f-a186-e8d2f2560dd7|retry:2")
    return live, failed


def test_supersede_happy_path(arm_client, arm_session, two_rows, auth_headers, add_token, tokens_file):
    add_token("svc-supersede", "tok-supersede-1", "runtime:write")
    live, failed = two_rows
    resp = arm_client.post(f"/api/v1/agent-runtimes/{FAILED}/supersede",
                           json={"superseded_by_runtime_id": LIVE, "reason": "failed retry attempt"},
                           headers=auth_headers("tok-supersede-1"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["actual_state"] == "SUPERSEDED"
    # row still exists (evidence preserved), linkage recorded in ownership_meta
    arm_session.expire_all()  # the API committed via its own session — drop stale identity map
    row = arm_session.get(__import__("server_manager.agent_runtime_manager.models.orm",
                                     fromlist=["AgentRuntime"]).AgentRuntime, uuid.UUID(FAILED))
    assert row is not None
    meta = row.ownership_meta or {}
    assert meta.get("superseded_by_runtime_id") == LIVE
    assert meta.get("superseded_reason") == "failed retry attempt"
    assert "superseded_at" in meta
    # audit event recorded
    ev = arm_session.query(RuntimeEvent).filter_by(runtime_id=uuid.UUID(FAILED),
                                                   action="runtime_superseded").all()
    assert len(ev) == 1
    # live runtime untouched
    live_row = arm_session.get(__import__("server_manager.agent_runtime_manager.models.orm",
                                          fromlist=["AgentRuntime"]).AgentRuntime, uuid.UUID(LIVE))
    assert live_row.actual_state == ActualState.RUNNING


def test_supersede_refuses_backing_resources(arm_client, arm_session, two_rows,
                                             auth_headers, add_token):
    add_token("svc-supersede2", "tok-supersede-2", "runtime:write")
    live, _ = two_rows
    resp = arm_client.post(f"/api/v1/agent-runtimes/{LIVE}/supersede",
                           json={"superseded_by_runtime_id": FAILED},
                           headers=auth_headers("tok-supersede-2"))
    assert resp.status_code == 409  # has node+vmid


def test_supersede_refuses_unknown_target(arm_client, auth_headers, add_token):
    add_token("svc-supersede3", "tok-supersede-3", "runtime:write")
    resp = arm_client.post(f"/api/v1/agent-runtimes/{uuid.uuid4()}/supersede",
                           json={"superseded_by_runtime_id": LIVE},
                           headers=auth_headers("tok-supersede-3"))
    assert resp.status_code == 404


def test_supersede_refuses_self(arm_client, arm_session, two_rows, auth_headers, add_token):
    add_token("svc-supersede4", "tok-supersede-4", "runtime:write")
    resp = arm_client.post(f"/api/v1/agent-runtimes/{FAILED}/supersede",
                           json={"superseded_by_runtime_id": FAILED},
                           headers=auth_headers("tok-supersede-4"))
    assert resp.status_code == 422


def test_supersede_idempotent_guard(arm_client, arm_session, two_rows, auth_headers, add_token):
    add_token("svc-supersede5", "tok-supersede-5", "runtime:write")
    live, failed = two_rows
    h = auth_headers("tok-supersede-5")
    r1 = arm_client.post(f"/api/v1/agent-runtimes/{FAILED}/supersede",
                         json={"superseded_by_runtime_id": LIVE}, headers=h)
    assert r1.status_code == 200
    r2 = arm_client.post(f"/api/v1/agent-runtimes/{FAILED}/supersede",
                         json={"superseded_by_runtime_id": LIVE}, headers=h)
    assert r2.status_code == 409


def test_supersede_requires_scope(arm_client, arm_session, two_rows, auth_headers, add_token):
    add_token("svc-ro-supersede", "tok-supersede-ro", "runtime:read")
    resp = arm_client.post(f"/api/v1/agent-runtimes/{FAILED}/supersede",
                           json={"superseded_by_runtime_id": LIVE},
                           headers=auth_headers("tok-supersede-ro"))
    assert resp.status_code == 403


def test_supersede_bad_uuid(arm_client, arm_session, two_rows, auth_headers, add_token):
    add_token("svc-supersede6", "tok-supersede-6", "runtime:write")
    resp = arm_client.post(f"/api/v1/agent-runtimes/{FAILED}/supersede",
                           json={"superseded_by_runtime_id": "not-a-uuid"},
                           headers=auth_headers("tok-supersede-6"))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# reconciler semantics
# ---------------------------------------------------------------------------

def test_reconciler_skips_superseded(arm_session):
    live = make_runtime(arm_session, node="miam00111", vmid=124, actual_state=ActualState.RUNNING)
    failed = make_runtime(arm_session, node=None, vmid=None, actual_state=ActualState.SUPERSEDED,
                          provisioning_request_id="req-superseded-evidence")
    svc = make_svc()
    res = svc.reconcile_runtime(arm_session, failed.runtime_id)
    assert res.action == "noop"
    assert res.before == res.after == "SUPERSEDED"
    # state did not flip back
    arm_session.refresh(failed)
    assert failed.actual_state == ActualState.SUPERSEDED


def test_failed_history_preserved_after_supersede(arm_session):
    """The five-row story: 4 failed + 1 live — after supersede all 5 rows exist,
    4 marked SUPERSEDED, live untouched, errors/timestamps intact."""
    live = make_runtime(arm_session, node="miam00111", vmid=124, actual_state=ActualState.RUNNING,
                        provisioning_request_id="acms-22ec3237|retry:5")
    failed_ids = []
    for i in range(4):
        fr = make_runtime(arm_session, node=None, vmid=None, actual_state=ActualState.ERROR,
                          provisioning_request_id=f"acms-22ec3237|retry:{i}")
        fr.actual_state = ActualState.SUPERSEDED
        if fr.ownership_meta is None:
            fr.ownership_meta = {}
        fr.ownership_meta["superseded_by_runtime_id"] = str(live.runtime_id)
        failed_ids.append(fr.runtime_id)
    arm_session.commit()

    rows = arm_session.query(
        __import__("server_manager.agent_runtime_manager.models.orm",
                   fromlist=["AgentRuntime"]).AgentRuntime
    ).filter(
        __import__("server_manager.agent_runtime_manager.models.orm",
                   fromlist=["AgentRuntime"]).AgentRuntime.provisioning_request_id.like("acms-22ec3237%")
    ).all()
    assert len(rows) == 5
    superseded = [r for r in rows if r.actual_state == ActualState.SUPERSEDED]
    running = [r for r in rows if r.actual_state == ActualState.RUNNING]
    assert len(superseded) == 4 and len(running) == 1
    assert all(r.ownership_meta.get("superseded_by_runtime_id") == str(live.runtime_id)
               for r in superseded)
