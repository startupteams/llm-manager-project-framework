"""PDU consumer integration tests (REV4 §10C; flight plan Phase 4).

Covers: read-only + dry-run pass-through, actuation gate (env flag),
identity-mismatch mapping, and the SM-level OFF refusal.
The PDU client is monkeypatched at the module boundary — CI never
touches real PDUs (PDU repo REQ-010).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class FakePduClient:
    calls: list[tuple] = []
    health_d = {"status": "ok", "backend_mode": "real"}
    caps_d = {"asset_count": 24, "supported_actions": ["on", "off", "reboot"]}
    assets_d = {"assets": [{"asset_id": "MIAM-00119", "outlet": 9, "ip": "10.0.20.151"}]}
    state_d = {"asset_id": "MIAM-00119", "power_state": "ON", "label": "MIAM-00119 - PVE node"}
    plan_d = {"actuated": False, "asset_id": "MIAM-00119", "action": "reboot",
              "current_state": "ON", "required_acks": ["acknowledge_controller_may_go_offline"]}
    job_d = {"job_id": "j-1", "state": "DONE"}
    audit_d = [{"event": "action_submitted", "asset_id": "MIAM-00119"}]

    def reset(self):
        self.calls = []

    def health(self):
        self.calls.append(("health",))
        return self.health_d

    def capabilities(self):
        self.calls.append(("caps",))
        return self.caps_d

    def list_assets(self):
        self.calls.append(("assets",))
        return self.assets_d

    def get_power_state(self, asset_id):
        self.calls.append(("state", asset_id))
        if asset_id == "MIAM-99999":
            from server_manager.common.pdu_manager_client import PduError
            raise PduError("unknown asset", status_code=404, body="ASSET_NOT_FOUND")
        return self.state_d["power_state"]

    def get_asset(self, asset_id):
        self.calls.append(("asset", asset_id))
        return {"asset_id": asset_id, "label": self.state_d["label"]}

    def plan_action(self, asset_id, action):
        self.calls.append(("plan", asset_id, action))
        return dict(self.plan_d, asset_id=asset_id, action=action)

    def get_job(self, job_id):
        self.calls.append(("job", job_id))
        return dict(self.job_d, job_id=job_id)

    def audit(self, lines=40):
        self.calls.append(("audit", lines))
        return list(self.audit_d)

    def submit_action(self, asset_id, action, reason, request_id, correlation_id=None,
                      source_service="svc-server-manager", expected_asset_id=None):
        self.calls.append(("submit", asset_id, action, request_id, expected_asset_id))
        return {"accepted": True, "job_id": "j-2"}


@pytest.fixture()
def client(monkeypatch):
    from server_manager.common import pdu_power
    from server_manager.common.auth import service_tokens
    import os, time, tempfile

    # tokens file
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".tokens", delete=False)
    tf.write("svc-acms=tok-acms-full-0001:ALL\n")
    tf.close()
    prev = os.environ.get("SERVER_MANAGER_SERVICE_TOKENS_FILE")
    if prev:
        os.environ["__SM_PREV_TOKENS_FILE__"] = prev
    os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = tf.name
    time.sleep(0.01)
    os.utime(tf.name)
    fake = FakePduClient()
    fake.reset()
    monkeypatch.setattr(pdu_power, "_client", lambda: fake)
    from server_manager.api_app import app

    yield TestClient(app), fake, tf.name
    # restore the previous tokens env so the session-scoped fixture stays coherent
    prev = os.environ.get("__SM_PREV_TOKENS_FILE__")
    if prev:
        os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = prev
        os.environ.pop("__SM_PREV_TOKENS_FILE__", None)
    else:
        os.environ.pop("SERVER_MANAGER_SERVICE_TOKENS_FILE", None)


AUTH = {"Authorization": "Bearer tok-acms-full-0001"}


def test_pdu_health_passthrough(client):
    c, fake, _ = client
    r = c.get("/api/v1/pdu/health", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


def test_pdu_requires_auth(client):
    c, fake, _ = client
    r = c.get("/api/v1/pdu/health")
    assert r.status_code == 401


def test_power_state_readonly(client):
    c, fake, _ = client
    r = c.get("/api/v1/pdu/assets/MIAM-00119/power-state", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["power_state"] == "ON"
    assert ("state", "MIAM-00119") in fake.calls


def test_power_state_unknown_asset_maps_502(client):
    c, fake, _ = client
    r = c.get("/api/v1/pdu/assets/MIAM-99999/power-state", headers=AUTH)
    assert r.status_code == 502


def test_dry_run_plan_never_actuates(client):
    c, fake, _ = client
    r = c.post("/api/v1/pdu/action-plans", headers=AUTH,
               json={"asset_id": "MIAM-00119", "action": "reboot"})
    assert r.status_code == 200
    assert r.json()["actuated"] is False
    assert not any(call[0] == "submit" for call in fake.calls)


def test_actuation_blocked_without_flag(client, monkeypatch):
    c, fake, _ = client
    monkeypatch.setenv("SERVER_MANAGER_PDU_ACTUATION", "")
    r = c.post("/api/v1/pdu/actions", headers=AUTH,
               json={"asset_id": "MIAM-00119", "action": "reboot", "reason": "test blocked",
                     "request_id": "req-blk-0001"})
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"]
    assert not any(call[0] == "submit" for call in fake.calls)


def test_actuation_enabled_submits_with_identity_guard(client, monkeypatch):
    c, fake, _ = client
    monkeypatch.setenv("SERVER_MANAGER_PDU_ACTUATION", "1")
    r = c.post("/api/v1/pdu/actions", headers=AUTH,
               json={"asset_id": "MIAM-00119", "action": "reboot", "reason": "authorized probe",
                     "request_id": "req-ok-0001", "correlation_id": "corr-1",
                     "source_service": "svc-acms"})
    assert r.status_code == 200, r.text
    submits = [call for call in fake.calls if call[0] == "submit"]
    assert submits and submits[0][1] == "MIAM-00119" and submits[0][2] == "reboot"
    assert submits[0][4] == "MIAM-00119"  # expected_asset_id guard sent


def test_off_action_refused_at_sm_level(client, monkeypatch):
    c, fake, _ = client
    monkeypatch.setenv("SERVER_MANAGER_PDU_ACTUATION", "1")
    r = c.post("/api/v1/pdu/actions", headers=AUTH,
               json={"asset_id": "MIAM-00119", "action": "off", "reason": "refused test",
                     "request_id": "req-off-0001"})
    assert r.status_code == 403
    assert not any(call[0] == "submit" for call in fake.calls)


def test_ensure_powered_on_skips_when_already_on(client, monkeypatch):
    from server_manager.common import pdu_power

    fake = FakePduClient(); fake.reset()
    monkeypatch.setattr(pdu_power, "_client", lambda: fake)
    monkeypatch.setenv("SERVER_MANAGER_PDU_ACTUATION", "1")
    res = pdu_power.ensure_powered_on("MIAM-00119", "already on check", "req-on-0001")
    assert res["action"] == "none_needed"
    assert not any(call[0] == "submit" for call in fake.calls)


def test_job_and_audit_passthrough(client):
    c, fake, _ = client
    r = c.get("/api/v1/pdu/jobs/j-1", headers=AUTH)
    assert r.status_code == 200 and r.json()["job_id"] == "j-1"
    r2 = c.get("/api/v1/pdu/audit?lines=10", headers=AUTH)
    assert r2.status_code == 200 and r2.json()["lines"]


def test_capabilities_passthrough(client):
    c, fake, _ = client
    r = c.get("/api/v1/pdu/capabilities", headers=AUTH)
    assert r.status_code == 200
    assert "supported_actions" in r.json()
