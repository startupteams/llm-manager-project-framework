"""Sandbox TTL tests (plan §26) — ARM side of the proxmox.sandbox.* domain.

Covers: sandbox creation stamps sandbox_expires_at + ownership_meta kind;
non-sandbox classes get NO TTL; reconcile_all sweeps expired sandboxes to
DESIRED_DESTROYED (API-only flip) with a durable sandbox_ttl_expired event;
extend-ttl endpoint bounds and audits; sandbox extend on a non-sandbox 422s.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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

from tests.server_manager.test_reconciliation import (  # noqa: E402
    ReconcileFakeProvider,
    bind_marker,
    make_runtime,
)


def make_settings():
    """Minimal settings stub for the reconciler (mirrors test_reconciliation)."""
    try:
        from tests.server_manager.test_reconciliation import make_settings as _ms
        return _ms()
    except ImportError:
        return ReconcileSettings()


def make_sandbox(session, **kw) -> AgentRuntime:
    """A sandbox-class runtime row (not provisioned — model-level tests)."""
    d = dict(
        acms_agent_id="agent-sandbox-0001",
        provisioning_request_id=f"req-{uuid.uuid4().hex[:12]}",
        name="sandbox-probe",
        runtime_class="sandbox",
        node=None, vmid=None,  # model-level: not provisioned
        desired_state=RuntimeState.DESIRED_RUNNING,
        actual_state=ActualState.ABSENT,
        created_by_agent_runtime_manager=True,
        template_source="miam00111/qemu/121",
    )
    d.update(kw)
    r = AgentRuntime(**d)
    session.add(r)
    session.commit()
    return r


# ------------------------------------------------------------------ stamping
def test_sandbox_create_stamps_ttl(arm_session):
    """submit() with runtime_class=sandbox stamps TTL + kind tag."""
    from server_manager.agent_runtime_manager.services.provisioning import (
        ProvisionRequest, ProvisioningService)

    svc = ProvisioningService()
    req = ProvisionRequest(
        acms_agent_id="agent-sandbox-0002", request_id=f"req-{uuid.uuid4().hex[:12]}",
        name="sbx-ttl-stamp", runtime_class="sandbox", sandbox_ttl_hours=4,
    )
    job, created = svc.submit(arm_session, req, requested_by="svc-test", authority="test")
    assert created
    runtime = arm_session.get(AgentRuntime, job.runtime_id)
    assert runtime.runtime_class == "sandbox"
    assert runtime.sandbox_expires_at is not None
    delta = runtime.sandbox_expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=3) < delta <= timedelta(hours=4, minutes=1)
    assert runtime.ownership_meta.get("kind") == "sandbox"
    assert runtime.ownership_meta.get("ttl_hours") == 4


def test_sandbox_ttl_bounded_by_settings(arm_session, monkeypatch):
    """Requested TTL is clamped to the settings max (72h default)."""
    from server_manager.common.config.settings import get_settings
    from server_manager.agent_runtime_manager.services.provisioning import (
        ProvisionRequest, ProvisioningService)

    monkeypatch.setenv("SERVER_MANAGER_ARM_SANDBOX_MAX_TTL_HOURS", "10")
    get_settings.cache_clear()
    try:
        svc = ProvisioningService()
        req = ProvisionRequest(
            acms_agent_id="agent-sandbox-0003", request_id=f"req-{uuid.uuid4().hex[:12]}",
            name="sbx-clamped", runtime_class="sandbox", sandbox_ttl_hours=999,
        )
        job, _ = svc.submit(arm_session, req, requested_by="svc-test", authority="test")
        runtime = arm_session.get(AgentRuntime, job.runtime_id)
        delta = runtime.sandbox_expires_at - datetime.now(timezone.utc)
        assert delta <= timedelta(hours=10, seconds=5)
        assert runtime.ownership_meta.get("ttl_hours") == 10
    finally:
        get_settings.cache_clear()


def test_worker_class_gets_no_ttl(arm_session):
    """Non-sandbox classes never carry a TTL (persistent runtimes are not TTL'd)."""
    from server_manager.agent_runtime_manager.services.provisioning import (
        ProvisionRequest, ProvisioningService)

    svc = ProvisioningService()
    req = ProvisionRequest(
        acms_agent_id="agent-sandbox-0004", request_id=f"req-{uuid.uuid4().hex[:12]}",
        name="persistent-no-ttl", runtime_class="software_development_worker",
        sandbox_ttl_hours=4,  # ignored for non-sandbox
    )
    job, _ = svc.submit(arm_session, req, requested_by="svc-test", authority="test")
    runtime = arm_session.get(AgentRuntime, job.runtime_id)
    assert runtime.sandbox_expires_at is None
    assert runtime.ownership_meta.get("kind") != "sandbox"


# ------------------------------------------------------------------ TTL sweep
def test_reconcile_all_expires_stale_sandbox(arm_session):
    """Expired sandbox -> DESIRED_DESTROYED + durable event; others untouched."""
    svc = ReconciliationService(provider=ReconcileFakeProvider(), settings=make_settings())
    stale = make_sandbox(arm_session, sandbox_expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    fresh = make_sandbox(arm_session, sandbox_expires_at=datetime.now(timezone.utc) + timedelta(hours=5))
    persistent = make_runtime(arm_session)

    svc.reconcile_all(arm_session)

    arm_session.refresh(stale); arm_session.refresh(fresh); arm_session.refresh(persistent)
    assert stale.desired_state == RuntimeState.DESIRED_DESTROYED
    assert fresh.desired_state == RuntimeState.DESIRED_RUNNING
    assert persistent.desired_state == RuntimeState.DESIRED_RUNNING
    events = arm_session.query(RuntimeEvent).filter_by(runtime_id=stale.runtime_id,
                                                       action="sandbox_ttl_expired").all()
    assert len(events) == 1


def test_ttl_sweep_idempotent(arm_session):
    """A second sweep does not re-flip or re-audit already-expired rows."""
    svc = ReconciliationService(provider=ReconcileFakeProvider(), settings=make_settings())
    stale = make_sandbox(arm_session, sandbox_expires_at=datetime.now(timezone.utc) - timedelta(hours=2))
    svc.expire_stale_sandboxes(arm_session)
    arm_session.commit()
    n1 = arm_session.query(RuntimeEvent).filter_by(runtime_id=stale.runtime_id,
                                                   action="sandbox_ttl_expired").count()
    svc.expire_stale_sandboxes(arm_session)
    arm_session.commit()
    n2 = arm_session.query(RuntimeEvent).filter_by(runtime_id=stale.runtime_id,
                                                   action="sandbox_ttl_expired").count()
    assert n1 == 1 and n2 == 1
    assert stale.desired_state == RuntimeState.DESIRED_DESTROYED


# ------------------------------------------------------------------ extend-ttl
def test_extend_ttl_bounds_and_audits(arm_schema):
    """extend-ttl: sandbox-only (422 otherwise), clamped, audited.

    Route test pattern (conftest arm_app): sessionmaker bound to the test
    transaction's connection — the route opens its own session.
    """
    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker

    from server_manager.agent_runtime_manager.api import v1 as v1mod

    class _Ident:
        service = "svc-test"

    make_sess = sessionmaker(bind=arm_schema["connection"],
                             join_transaction_mode="create_savepoint",
                             expire_on_commit=False, future=True)
    with make_sess() as seed:
        sbx = make_sandbox(seed)
        persistent = make_runtime(seed)
        sbx_id, persistent_id = str(sbx.runtime_id), str(persistent.runtime_id)

    monkeypatch_factory = sessionmaker(bind=arm_schema["connection"],
                                       join_transaction_mode="create_savepoint",
                                       expire_on_commit=False, future=True)

    original = v1mod.get_session_factory
    v1mod.get_session_factory = lambda *a, **k: monkeypatch_factory
    try:
        # non-sandbox -> 422
        with pytest.raises(HTTPException) as ei:
            v1mod.extend_sandbox_ttl(persistent_id,
                                     v1mod.ExtendTtlRequest(ttl_hours=5, reason="test extend"),
                                     ident=_Ident())
        assert ei.value.status_code == 422

        # bounded extend
        out = v1mod.extend_sandbox_ttl(sbx_id,
                                       v1mod.ExtendTtlRequest(ttl_hours=500, reason="test extend"),
                                       ident=_Ident())
        assert out["ttl_hours"] <= 72  # settings default max
        events = 0
        with make_sess() as check:
            sbx2 = check.get(AgentRuntime, sbx_id)
            assert sbx2.sandbox_expires_at is not None
            assert sbx2.ownership_meta.get("ttl_hours") == out["ttl_hours"]
            events = check.query(RuntimeEvent).filter_by(
                runtime_id=sbx2.runtime_id, action="sandbox_ttl_extended").count()
        assert events == 1

        # unknown runtime -> 404
        with pytest.raises(HTTPException) as ei2:
            v1mod.extend_sandbox_ttl(str(uuid.uuid4()),
                                     v1mod.ExtendTtlRequest(ttl_hours=5, reason="test extend"),
                                     ident=_Ident())
        assert ei2.value.status_code == 404
    finally:
        v1mod.get_session_factory = original
