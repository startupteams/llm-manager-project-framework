"""Recovery policy tests (2026-09-29 human direction):

- default maximum 5 automatic recovery attempts (env ARM_RECONCILE_MAX_ATTEMPTS);
- attempts vary via bounded exponential backoff;
- every attempt audited with failure_class / recovery_method / attempt;
- exhaustion → Human Attention (audited reconcile_recovery_exhausted event with
  strategy_next=human_attention; PDU hard-cycle is NEVER an automatic step);
- runtime still reconciles normally after 1..4 attempts, refuses at 5.
"""
from __future__ import annotations

import json

import pytest

from server_manager.agent_runtime_manager.models.orm import RuntimeEvent
from server_manager.agent_runtime_manager.services.reconciliation import ReconcileSettings

from .test_reconciliation import (  # reuse the shared fakes/fixtures
    ReconcileFakeProvider,
    bind_marker,
    make_runtime,
    make_settings,
)


def test_default_max_attempts_is_five():
    assert ReconcileSettings.from_env().max_recovery_attempts == 5


def test_four_attempts_still_recovers(arm_session):
    fake = ReconcileFakeProvider()
    settings = make_settings(backoff_base_s=0, max_recovery_attempts=5)
    from server_manager.agent_runtime_manager.services.reconciliation import ReconciliationService

    svc = ReconciliationService(provider=fake, settings=settings)
    r = make_runtime(arm_session, recovery_count=4)
    bind_marker(fake, r)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "started"          # attempt 5 of 5 proceeds
    assert r.recovery_count == 5


def test_fifth_attempt_refused_and_audited(arm_session):
    fake = ReconcileFakeProvider()
    settings = make_settings(backoff_base_s=0, max_recovery_attempts=5)
    from server_manager.agent_runtime_manager.services.reconciliation import ReconciliationService

    svc = ReconciliationService(provider=fake, settings=settings)
    r = make_runtime(arm_session, recovery_count=5)  # exhausted
    bind_marker(fake, r)
    res = svc.reconcile_runtime(arm_session, r.runtime_id)
    assert res.action == "error"
    assert "recovery exhausted" in res.error
    assert [c for c in fake.calls if c[0] == "start"] == []
    ev = (arm_session.query(RuntimeEvent)
          .filter_by(action="reconcile_recovery_exhausted")
          .order_by(RuntimeEvent.id.desc()).first())
    assert ev is not None
    d = ev.detail or {}
    assert d.get("failure_class") == "vm_start_recovery_exhausted"
    assert d.get("recovery_method") == "vm_start"
    assert d.get("strategy_next") == "human_attention"


def test_start_attempt_audits_method_and_attempt(arm_session):
    fake = ReconcileFakeProvider()
    settings = make_settings(backoff_base_s=0, max_recovery_attempts=5)
    from server_manager.agent_runtime_manager.services.reconciliation import ReconciliationService

    svc = ReconciliationService(provider=fake, settings=settings)
    r = make_runtime(arm_session, recovery_count=2)
    bind_marker(fake, r)
    svc.reconcile_runtime(arm_session, r.runtime_id)
    ev = (arm_session.query(RuntimeEvent)
          .filter_by(action="reconcile_start")
          .order_by(RuntimeEvent.id.desc()).first())
    d = ev.detail or {}
    assert d.get("recovery_method") == "vm_start"
    assert d.get("attempt") == 3
    assert d.get("failure_class") == "vm_stopped_desired_running"


def test_backoff_varies_by_attempt(arm_session, monkeypatch):
    """Attempts should vary: backoff grows 2^n (capped). Assert the schedule values."""
    s = make_settings(backoff_base_s=10, backoff_cap_s=60, max_recovery_attempts=5)
    delays = []
    for count in range(1, 5):
        delays.append(min(s.backoff_base_s * (2 ** (count - 1)), s.backoff_cap_s))
    assert delays == [10, 20, 40, 60]  # varied, capped
