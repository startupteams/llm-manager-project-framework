"""Machine API auth + route behavior tests (REV4 §10/§11)."""
from __future__ import annotations

import json
import time
import uuid

import pytest

AUTH_FULL = {"Authorization": "Bearer tok-acms-full-0001"}  # svc-acms, ALL scopes (conftest)


def test_401_without_bearer(arm_client, tokens_file):
    r = arm_client.post("/api/v1/agent-runtimes", json={})
    assert r.status_code == 401
    r = arm_client.get("/api/v1/agent-runtimes")
    assert r.status_code == 401


def test_401_with_invalid_token(arm_client, tokens_file):
    r = arm_client.get("/api/v1/agent-runtimes", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_403_scope_gated(arm_client, tokens_file, add_token):
    add_token("svc-readonly", "tok-readonly-0001", "runtime:read")
    r = arm_client.get("/api/v1/agent-runtimes", headers={"Authorization": "Bearer tok-readonly-0001"})
    assert r.status_code == 200  # read allowed
    r2 = arm_client.post("/api/v1/agent-runtimes", json={}, headers={"Authorization": "Bearer tok-readonly-0001"})
    assert r2.status_code == 403  # write refused


def test_create_runtime_and_get(arm_client, arm_session, tokens_file, monkeypatch):
    # no-op run_job so no VM work happens
    from server_manager.agent_runtime_manager.services.provisioning import ProvisioningService

    monkeypatch.setattr(ProvisioningService, "run_job", lambda self, session, job_id: None)
    body = {
        "acms_agent_id": "agent-api-0001",
        "request_id": "req-api-0001",
        "name": "api-worker",
    }
    r = arm_client.post("/api/v1/agent-runtimes", json=body, headers=AUTH_FULL)
    assert r.status_code == 202, r.text
    d = r.json()
    assert d["created"] is True
    assert d["job"]["state"] == "QUEUED"  # run_job was a no-op
    # idempotent repeat
    r2 = arm_client.post("/api/v1/agent-runtimes", json=body, headers=AUTH_FULL)
    assert r2.json()["created"] is False


def test_get_runtime_404(arm_client, tokens_file):
    r = arm_client.get(f"/api/v1/agent-runtimes/{uuid.uuid4()}", headers=AUTH_FULL)
    assert r.status_code == 404


def test_capabilities_requires_auth(arm_client, tokens_file):
    r = arm_client.get("/api/v1/capabilities")
    assert r.status_code == 401
    r = arm_client.get("/api/v1/capabilities", headers=AUTH_FULL)
    assert r.status_code == 200
    assert r.json()["api_contract_version"] == "1.0.0"


def test_desired_state_set_and_audit(arm_client, arm_session, tokens_file):
    from sqlalchemy import select

    from server_manager.agent_runtime_manager.models.orm import AgentRuntime, RuntimeEvent

    rt = AgentRuntime(acms_agent_id="agent-ds-00001", provisioning_request_id="req-ds-0001",
                      name="ds-worker", created_by_agent_runtime_manager=True)
    arm_session.add(rt)
    arm_session.commit()
    r = arm_client.post(f"/api/v1/agent-runtimes/{rt.runtime_id}/desired-state",
                        json={"desired_state": "DESIRED_STOPPED", "reason": "maintenance window"},
                        headers=AUTH_FULL)
    assert r.status_code == 200, r.text
    assert r.json()["desired_state"] == "DESIRED_STOPPED"
    ev = arm_session.execute(select(RuntimeEvent).where(RuntimeEvent.action == "desired_state_set")).scalars().all()
    assert len(ev) == 1


def test_delete_refused_for_foreign_vm(arm_client, arm_session, tokens_file, monkeypatch):
    """The §8 guard through the API: a VM without our marker must 403 + audit."""
    from server_manager.agent_runtime_manager.providers.proxmox_vm import OwnershipError, ProxmoxVMProvider, verify_ownership

    def refuse(self, node, vmid, runtime, protected, require_protection_check=True):
        raise OwnershipError("ownership marker mismatch")

    monkeypatch.setattr("server_manager.agent_runtime_manager.api.v1.verify_ownership", refuse)
    from server_manager.agent_runtime_manager.models.orm import AgentRuntime

    rt = AgentRuntime(acms_agent_id="agent-del-0001", provisioning_request_id="req-del-0001",
                      name="del-worker", created_by_agent_runtime_manager=True,
                      vmid=555, node="miam00111")
    arm_session.add(rt)
    arm_session.commit()
    r = arm_client.delete(f"/api/v1/agent-runtimes/{rt.runtime_id}", headers=AUTH_FULL)
    assert r.status_code == 403, r.text
    from sqlalchemy import select

    from server_manager.agent_runtime_manager.models.orm import RuntimeEvent

    ev = arm_session.execute(select(RuntimeEvent).where(RuntimeEvent.action == "destroy_refused")).scalars().all()
    assert len(ev) == 1
