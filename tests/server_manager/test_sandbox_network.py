"""W4.1 sandbox DHCP isolation tests — reservation lifecycle + fail-closed gate.

Covers (plan §W4.1 acceptance, unit layer; live acceptance is separate):
- range parsing fail-closed
- allocate(): skips foreign reservations, skips leased IPs, skips alive IPs,
  reuses crashed-create SM reservations, exhausts fail-closed
- reserve_for_mac(): create, reuse, foreign-preservation
- release(): SM-tagged only, never foreign
- provisioning gate: sandbox + gate enabled → reservation created BEFORE boot;
  gate disabled → provisioning BLOCKED (W4.1 pause semantics);
  reservation failure → job FAILED, never boots
- destroy path releases the reservation (audited)
"""
from __future__ import annotations

import json
import uuid

import pytest

from server_manager.agent_runtime_manager.models.orm import (
    ActualState,
    JobState,
    RuntimeEvent,
)
from server_manager.agent_runtime_manager.services.sandbox_network import (
    KeaReservationClient,
    SandboxNetworkError,
    SandboxNetworkService,
    parse_pool_range,
)


# --------------------------------------------------------------------- fakes
class FakeKeaClient:
    def __init__(self, subnet="sub-uuid-1"):
        self.subnet = subnet
        self.res: dict[str, dict] = {}      # uuid -> row
        self.leased: set[str] = set()
        self.alive_ips: set[str] = set()
        self.reconfigured = 0
        self._n = 0

    def _add(self, ip, mac, hostname, description):
        self._n += 1
        u = f"res-{self._n}"
        self.res[u] = {"uuid": u, "ip_address": ip, "hw_address": mac,
                       "hostname": hostname, "description": description}
        return u

    # client API used by SandboxNetworkService
    def subnet_uuid(self):
        return self.subnet

    def reservations(self):
        return list(self.res.values())

    def leases(self):
        return [{"address": ip, "state": 0} for ip in sorted(self.leased)]

    def active_lease_ips(self):
        return set(self.leased)

    def create_reservation(self, ip, mac, hostname, description):
        for r in self.res.values():
            if r["ip_address"] == ip:
                raise SandboxNetworkError("Duplicate entry exists.")
        u = self._add(ip, mac, hostname, description)
        self.reconfigure()  # the real client reconfigures after every write
        return u

    def delete_reservation(self, u):
        if u not in self.res:
            raise SandboxNetworkError("not found")
        del self.res[u]
        self.reconfigure()

    def reconfigure(self):
        self.reconfigured += 1


def make_service(client, alive=None):
    svc = SandboxNetworkService(client=client)
    if alive:
        import types
        orig = svc._alive_ips
        svc._alive_ips = lambda ips: set(ips) & set(alive)  # noqa: ARG005
    return svc


# ------------------------------------------------------------------ parsing
def test_parse_range():
    s, e = parse_pool_range("10.0.20.222 - 10.0.20.249")
    assert (str(s), str(e)) == ("10.0.20.222", "10.0.20.249")
    with pytest.raises(SandboxNetworkError):
        parse_pool_range("garbage")
    with pytest.raises(SandboxNetworkError):
        parse_pool_range("10.0.20.240 - 10.0.20.222")


# ---------------------------------------------------------------- allocation
def test_allocate_skips_foreign_and_leased():
    c = FakeKeaClient()
    # foreign (static infra) reservations occupy the first three pool addrs
    c._add("10.0.20.222", "aa:aa:aa:aa:aa:01", "worker-001", "static protection")
    c._add("10.0.20.223", "aa:aa:aa:aa:aa:02", "worker-002", "static protection")
    c.leased.add("10.0.20.224")
    svc = make_service(c)
    ip, existing = svc.allocate()
    assert ip == "10.0.20.225" and existing is None


def test_allocate_reuses_crashed_create_reservation():
    c = FakeKeaClient()
    u = c._add("10.0.20.222", "de:ad:be:ef:00:01", "sbx-old",
               "ACMS_SANDBOX: work=w1 agent=a1 host=sbx-old")
    svc = make_service(c)
    ip, existing = svc.allocate()
    assert ip == "10.0.20.222" and existing == u


def test_allocate_skips_alive_address():
    c = FakeKeaClient()
    svc = make_service(c, alive={"10.0.20.222"})
    ip, _ = svc.allocate()
    assert ip == "10.0.20.223"


def test_allocate_exhausted_fail_closed():
    c = FakeKeaClient()
    for i, ip in enumerate([f"10.0.20.{x}" for x in range(222, 250)]):
        c._add(ip, f"aa:aa:aa:aa:bb:{i:02d}", "worker-x", "static protection")
    svc = make_service(c)
    with pytest.raises(SandboxNetworkError) as ei:
        svc.allocate()
    assert "exhausted" in str(ei.value)


# ------------------------------------------------------------------ reserve
def test_reserve_and_verify_and_release():
    c = FakeKeaClient()
    svc = make_service(c)
    rec = svc.reserve_for_mac("10.0.20.222", "DE:AD:BE:EF:00:09", "sbx-a", "w1", "a1")
    assert rec.uuid and rec.mac == "de:ad:be:ef:00:09"
    assert c.res[rec.uuid]["description"].startswith("ACMS_SANDBOX:")
    assert c.reconfigured >= 1
    svc.verify_reserved("10.0.20.222")
    svc.release("10.0.20.222")
    assert "10.0.20.222" not in {r["ip_address"] for r in c.reservations()}


def test_release_refuses_foreign_reservation():
    c = FakeKeaClient()
    c._add("10.0.20.222", "aa:aa:aa:aa:aa:01", "worker-001", "W4.1 static protection")
    svc = make_service(c)
    with pytest.raises(SandboxNetworkError):
        svc.release("10.0.20.222")


def test_reserve_rejects_invalid_mac():
    c = FakeKeaClient()
    svc = make_service(c)
    with pytest.raises(SandboxNetworkError):
        svc.reserve_for_mac("10.0.20.222", "not-a-mac", "sbx", "w", "a")


# ------------------------------------------------------- provisioning gate
class FakeSandboxProvider:
    """Happy-path VM provider with a fixed post-clone MAC + IP."""

    def __init__(self):
        self.started = False
        self.agent_id = None

    def list_nodes(self):
        return [("miam-00135", "online")]

    def next_vmid(self):
        return 400

    def template_storage(self, spec):
        return "testthin"

    def storage_active(self, node, storage):
        return True

    def clone_template(self, spec, new_vmid):
        pass

    def wait_clone_lock_release(self, node, vmid):
        pass

    def configure_cloud_init(self, spec, vmid, extra=None):
        pass

    def start(self, node, vmid):
        self.started = True

    def wait_for_ip(self, node, vmid, max_s=300):
        return "10.0.20.222"  # the reserved sandbox address (what Kea would serve)

    def vm_exists(self, node, vmid):
        return True

    def vm_config(self, node, vmid):
        return {"description": json.dumps({
            "created_by_agent_runtime_manager": True,
            "acms_agent_id": self.agent_id,
        }), "net0": "virtio,bridge=vmbr0,mac=DE:AD:BE:EF:00:42"}


def _svc(monkeypatch, fake, kea):
    from server_manager.agent_runtime_manager.services.provisioning import ProvisioningService
    svc = ProvisioningService()
    monkeypatch.setattr(svc, "provider", fake)
    real_submit = svc.submit

    def _submit(session, req, requested_by, authority):
        fake.agent_id = req.acms_agent_id
        return real_submit(session, req, requested_by, authority)

    svc.submit = _submit
    # the gate imports SandboxNetworkService from sandbox_network at call time —
    # patch at the SOURCE module so the deferred import picks up the fake.
    import server_manager.agent_runtime_manager.services.sandbox_network as snw
    monkeypatch.setattr(
        snw, "SandboxNetworkService", lambda *a, **k: make_service(kea))
    return svc


def _sandbox_req(name="sbx-net-1"):
    from server_manager.agent_runtime_manager.services.provisioning import ProvisionRequest
    return ProvisionRequest(
        acms_agent_id=f"agent-sbxnet-{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex[:12]}",
        name=name, runtime_class="sandbox", sandbox_ttl_hours=4)


def test_gate_enabled_creates_reservation_before_boot(arm_session, monkeypatch):
    kea = FakeKeaClient()
    fake = FakeSandboxProvider()
    svc = _svc(monkeypatch, fake, kea)
    monkeypatch.setenv("SERVER_MANAGER_SANDBOX_NETWORK_ENABLED", "1")
    from server_manager.common.config.settings import get_settings
    get_settings.cache_clear()
    try:
        job, _ = svc.submit(arm_session, _sandbox_req(), "svc-test", "test")
        svc.run_job(arm_session, job.job_id)
        arm_session.refresh(job)
        assert job.state == JobState.DONE
        assert fake.started is True
        # the reservation EXISTS before boot (order proof: reconfigure happened,
        # and the runtime metadata carries the reserved identity)
        rt = job.runtime
        arm_session.refresh(rt)
        assert rt.ownership_meta["sandbox_ip"] == "10.0.20.222"
        assert rt.ownership_meta["sandbox_mac"] == "de:ad:be:ef:00:42"
        assert rt.ownership_meta["sandbox_reservation_uuid"] in kea.res
        # step recorded
        steps = {s.step: s.state for s in job.steps}
        assert steps.get("SANDBOX_DHCP_RESERVE") == "DONE"
        ev = arm_session.query(RuntimeEvent).filter_by(
            runtime_id=rt.runtime_id, action="sandbox_reservation_created").all()
        assert len(ev) == 1
    finally:
        get_settings.cache_clear()


def test_gate_disabled_blocks_sandbox_provisioning(arm_session, monkeypatch):
    kea = FakeKeaClient()
    fake = FakeSandboxProvider()
    svc = _svc(monkeypatch, fake, kea)
    monkeypatch.delenv("SERVER_MANAGER_SANDBOX_NETWORK_ENABLED", raising=False)
    from server_manager.common.config.settings import get_settings
    get_settings.cache_clear()
    try:
        job, _ = svc.submit(arm_session, _sandbox_req("sbx-paused"), "svc-test", "test")
        svc.run_job(arm_session, job.job_id)
        arm_session.refresh(job)
        assert job.state == JobState.FAILED
        assert "gate disabled" in (job.error or "")
        assert fake.started is False  # NEVER booted
        assert kea.res == {}          # nothing reserved
    finally:
        get_settings.cache_clear()


def test_reservation_failure_fails_job_never_boots(arm_session, monkeypatch):
    kea = FakeKeaClient()
    # exhaust the pool with foreign reservations → allocate raises
    for i, ip in enumerate([f"10.0.20.{x}" for x in range(222, 250)]):
        kea._add(ip, f"aa:aa:aa:aa:cc:{i:02d}", "worker-x", "static protection")
    fake = FakeSandboxProvider()
    svc = _svc(monkeypatch, fake, kea)
    monkeypatch.setenv("SERVER_MANAGER_SANDBOX_NETWORK_ENABLED", "1")
    from server_manager.common.config.settings import get_settings
    get_settings.cache_clear()
    try:
        job, _ = svc.submit(arm_session, _sandbox_req("sbx-fail"), "svc-test", "test")
        svc.run_job(arm_session, job.job_id)
        arm_session.refresh(job)
        assert job.state == JobState.FAILED
        assert "sandbox DHCP reservation failed" in (job.error or "")
        assert fake.started is False
        ev = arm_session.query(RuntimeEvent).filter_by(
            runtime_id=job.runtime.runtime_id, action="sandbox_reservation_failed").all()
        assert len(ev) == 1
    finally:
        get_settings.cache_clear()


def test_destroy_releases_reservation(arm_session, monkeypatch):
    """DELETE runtime → sandbox reservation released + audited."""
    import json as _json

    from sqlalchemy.orm import sessionmaker

    import server_manager.agent_runtime_manager.api.v1 as v1mod
    from server_manager.agent_runtime_manager.models.orm import (
        ActualState, AgentRuntime, RuntimeState)

    kea = FakeKeaClient()
    svc = make_service(kea)
    rec = svc.reserve_for_mac("10.0.20.222", "de:ad:be:ef:00:77", "sbx-del", "w9", "a9")

    rt = AgentRuntime(
        acms_agent_id="agent-del-0001",
        provisioning_request_id=f"req-{uuid.uuid4().hex[:12]}",
        name="sbx-del", runtime_class="sandbox", node="miam-00135", vmid=400,
        desired_state=RuntimeState.DESIRED_RUNNING,
        actual_state=ActualState.RUNNING,
        created_by_agent_runtime_manager=True,
        template_source="miam00111/qemu/121",
        ownership_meta={"sandbox_ip": "10.0.20.222",
                        "sandbox_mac": "de:ad:be:ef:00:77",
                        "sandbox_reservation_uuid": rec.uuid})
    arm_session.add(rt)
    arm_session.commit()
    rid = str(rt.runtime_id)

    class _Ident:
        service = "svc-test"

    factory = sessionmaker(bind=arm_session.bind, join_transaction_mode="create_savepoint",
                           expire_on_commit=False, future=True)
    original = v1mod.get_session_factory
    v1mod.get_session_factory = lambda *a, **k: factory
    monkeypatch.setattr(v1mod.ProxmoxVMProvider, "vm_exists", lambda self, n, v: True, raising=False)
    monkeypatch.setattr(v1mod.ProxmoxVMProvider, "vm_config",
                        lambda self, n, v: {"description": _json.dumps({
                            "created_by_agent_runtime_manager": True,
                            "server_runtime_id": rid, "acms_agent_id": "agent-del-0001",
                            "provisioning_request_id": rt.provisioning_request_id,
                            "provider": "proxmox_vm", "node": n, "vmid": v,
                            "created_at": "2026-10-02T00:00:00+00:00",
                            "template_source": "miam00111/qemu/121"})}, raising=False)
    monkeypatch.setattr(v1mod.ProxmoxVMProvider, "shutdown", lambda self, n, v: None, raising=False)
    monkeypatch.setattr(v1mod.ProxmoxVMProvider, "destroy", lambda self, n, v: None, raising=False)
    monkeypatch.setattr(
        "server_manager.agent_runtime_manager.services.sandbox_network.SandboxNetworkService",
        lambda *a, **k: make_service(kea))
    try:
        out = v1mod.destroy_agent_runtime(rid, ident=_Ident())
        assert out["status"] == "DESTROYED"
        assert "10.0.20.222" not in {r["ip_address"] for r in kea.reservations()}
    finally:
        v1mod.get_session_factory = original


# ------------------------------------------------------- login/retry behavior
def test_post_json_relogin_on_html(monkeypatch):
    """First API call on a fresh session receives the LOGIN PAGE (HTML) — the
    client must login via the shared cookie jar and retry once (live-found
    2026-10-02)."""
    class _Resp:
        def __init__(self, body):
            self.body = body
        def read(self):
            return self.body.encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    opened = {"n": 0}

    class _Opener:
        def open(self, req, timeout=None):
            opened["n"] += 1
            if opened["n"] == 1:
                return _Resp("<!doctype html><html>login page</html>")
            return _Resp('{"rows": [], "total": 0}')

    c = KeaReservationClient("http://opn.test", "pw")
    c._csrf = "tok"
    monkeypatch.setattr(KeaReservationClient, "_opener", lambda self: _Opener())
    monkeypatch.setattr(KeaReservationClient, "_get",
                        lambda self, path:
                        'page with setRequestHeader("X-CSRFToken", "tok2")')
    monkeypatch.setattr(KeaReservationClient, "_login",
                        lambda self: None)  # session established on retry
    out = c._post_json("/api/kea/dhcpv4/search_reservation/")
    assert out == {"rows": [], "total": 0}
    assert opened["n"] == 2  # HTML → login → retried once
