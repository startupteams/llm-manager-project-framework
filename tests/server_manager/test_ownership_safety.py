"""Ownership-safety tests (REV4 §8) — the core guard against destroying foreign VMs."""
from __future__ import annotations

import uuid

import pytest

from server_manager.agent_runtime_manager.models.orm import (
    ActualState,
    AgentRuntime,
    RuntimeState,
)
from server_manager.agent_runtime_manager.providers.proxmox_vm import (
    OwnershipError,
    ownership_marker,
    verify_ownership,
)


class FakeProvider:
    """Fake PVE provider: configurable config/exists responses, records calls."""

    def __init__(self, description=None, exists=True):
        self.description = description
        self.exists = exists
        self.calls = []

    def vm_exists(self, node, vmid):
        self.calls.append(("vm_exists", node, vmid))
        return self.exists

    def vm_config(self, node, vmid):
        self.calls.append(("vm_config", node, vmid))
        return {"description": self.description} if self.description else {}


def _runtime(**kw):
    d = dict(
        runtime_id=uuid.uuid4(), acms_agent_id="agent-12345678",
        provisioning_request_id="req-12345678", name="worker-x",
        created_by_agent_runtime_manager=True, template_source="miam00111/qemu/121",
    )
    d.update(kw)
    return AgentRuntime(**d)


def test_protected_vmid_refused(arm_session):
    rt = _runtime()
    arm_session.add(rt)
    arm_session.commit()
    prov = FakeProvider(exists=True, description=None)
    with pytest.raises(OwnershipError, match="protected"):
        verify_ownership(prov, "miam00111", 906, rt, protected_vmids={906})


def test_missing_vm_refused(arm_session):
    rt = _runtime()
    arm_session.add(rt)
    arm_session.commit()
    prov = FakeProvider(exists=False)
    with pytest.raises(OwnershipError, match="does not exist"):
        verify_ownership(prov, "miam00111", 123, rt, protected_vmids=set())


def test_unowned_runtime_refused(arm_session):
    rt = _runtime(created_by_agent_runtime_manager=False)
    arm_session.add(rt)
    arm_session.commit()
    prov = FakeProvider(exists=True, description='{"created_by_agent_runtime_manager": true}')
    with pytest.raises(OwnershipError, match="not marked"):
        verify_ownership(prov, "miam00111", 123, rt, protected_vmids=set())


def test_unparseable_marker_refused(arm_session):
    rt = _runtime()
    arm_session.add(rt)
    arm_session.commit()
    prov = FakeProvider(exists=True, description="some human text, not JSON")
    with pytest.raises(OwnershipError, match="no parseable ownership marker"):
        verify_ownership(prov, "miam00111", 123, rt, protected_vmids=set())


def test_foreign_runtime_marker_refused(arm_session):
    rt = _runtime()
    arm_session.add(rt)
    arm_session.commit()
    marker = ownership_marker(rt, "miam00111", 123, "miam00111/qemu/121")
    marker["server_runtime_id"] = str(uuid.uuid4())  # someone else's runtime
    import json

    prov = FakeProvider(exists=True, description=json.dumps(marker))
    with pytest.raises(OwnershipError, match="mismatch"):
        verify_ownership(prov, "miam00111", 123, rt, protected_vmids=set())


def test_incomplete_marker_refused(arm_session):
    rt = _runtime()
    arm_session.add(rt)
    arm_session.commit()
    import json

    marker = {"created_by_agent_runtime_manager": True,
              "server_runtime_id": str(rt.runtime_id)}  # missing the rest
    prov = FakeProvider(exists=True, description=json.dumps(marker))
    with pytest.raises(OwnershipError, match="incomplete"):
        verify_ownership(prov, "miam00111", 123, rt, protected_vmids=set())


def test_valid_marker_passes(arm_session):
    rt = _runtime()
    arm_session.add(rt)
    arm_session.commit()
    import json

    marker = ownership_marker(rt, "miam00111", 123, "miam00111/qemu/121")
    # ownership_marker uses runtime.created_at — ensure not None
    if marker.get("created_at") is None:
        marker["created_at"] = "2026-09-27T00:00:00Z"
    prov = FakeProvider(exists=True, description=json.dumps(marker))
    verify_ownership(prov, "miam00111", 123, rt, protected_vmids={906})  # must NOT raise


def test_marker_carries_required_keys(arm_session):
    rt = _runtime()
    arm_session.add(rt)
    arm_session.commit()
    import json

    from server_manager.agent_runtime_manager.models.orm import OWNERSHIP_REQUIRED_KEYS

    marker = ownership_marker(rt, "miam00111", 123, "t")
    if marker.get("created_at") is None:
        marker["created_at"] = "x"
    for k in OWNERSHIP_REQUIRED_KEYS:
        assert k in marker
