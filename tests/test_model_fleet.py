"""Model Fleet tests — window-5 plan §3/§15.

Covers the §15 fleet matrix: multiple VMs render, stopped VM renders honestly,
unreachable model service ≠ VM offline, multiple models, stale registration,
state derivation (powered-on vs model-healthy are separate facts).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "service", "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

import fleet_view as fv
from fleet_view import (MISCONFIGURED, ONLINE_IDLE, SERVING, STALE,
                        STOPPED_EXPECTED, UNREACHABLE, _derive_state)


class TestDeriveState:
    def test_healthy_and_serving(self):
        s = _derive_state("healthy", "RUNNING", "SERVING",
                          [{"routable": True, "is_alias": False,
                            "last_verified": datetime.now(timezone.utc).isoformat()}])
        assert s == SERVING

    def test_unreachable_with_desired_running(self):
        assert _derive_state("error", "RUNNING", "SERVING", []) == UNREACHABLE

    def test_stopped_intentional_is_expected_not_error(self):
        assert _derive_state("error", "STOPPED_INTENTIONAL", "MAINTENANCE", []) == STOPPED_EXPECTED
        assert _derive_state("error", "STOPPED", "MAINTENANCE", []) == STOPPED_EXPECTED

    def test_reachable_but_maintenance_is_online_idle(self):
        assert _derive_state("healthy", "RUNNING", "MAINTENANCE", []) == ONLINE_IDLE

    def test_reachable_but_no_routable_models_is_online_idle(self):
        s = _derive_state("healthy", "RUNNING", "SERVING",
                          [{"routable": False, "is_alias": False}])
        assert s == ONLINE_IDLE

    def test_http_error_is_misconfigured(self):
        assert _derive_state("http_503", "RUNNING", "SERVING", []) == MISCONFIGURED

    def test_stale_registration(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        s = _derive_state("healthy", "RUNNING", "SERVING",
                          [{"routable": True, "is_alias": False, "last_verified": old}])
        assert s == STALE

    def test_aliases_do_not_mask_stale(self):
        """A fresh alias row must not hide a stale real model's staleness."""
        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        s = _derive_state("healthy", "RUNNING", "SERVING",
                          [{"routable": True, "is_alias": True, "last_verified": None},
                           {"routable": True, "is_alias": False, "last_verified": old}])
        assert s == STALE


class TestFleetViewVisibility:
    def test_all_inventory_ips_present(self, monkeypatch):
        """Every inventory row stays visible — including stopped/error hosts."""
        def fake_inventory():
            return {
                "10.0.20.168": {"host_id": 6, "inventory_name": "MIAM-00111 / VM102",
                                 "gpu_count": 6, "gpu_model": "RTX 3080 20GB",
                                 "node": "miam00111", "vmid": 102,
                                 "desired_power_state": "RUNNING",
                                 "desired_service_state": "SERVING",
                                 "management_mode": "MANAGED"},
                "10.0.20.161": {"host_id": 1, "inventory_name": "MIAM-00111 / VM103",
                                 "gpu_count": 6, "gpu_model": "RTX 3080 20GB",
                                 "node": "miam00111", "vmid": 103,
                                 "desired_power_state": "STOPPED_INTENTIONAL",
                                 "desired_service_state": "MAINTENANCE",
                                 "management_mode": "MANAGED"},
            }
        def fake_models():
            return {"10.0.20.168": [{"model": "qwen3.8-flash-next", "host_id": 6,
                                      "engine": "vllm", "health": "healthy",
                                      "routable": True, "is_alias": False,
                                      "alias_target": None, "context_limit": 70000,
                                      "backend_url": "http://10.0.20.168:8000/v1",
                                      "last_verified": datetime.now(timezone.utc).isoformat()}]}
        def fake_probe(h):
            if h["ip"] == "10.0.20.161":
                return {"name": h["name"], "ip": h["ip"], "port": 8000,
                        "status": "error", "error": "No route to host", "latency_ms": 3000}
            return {"name": h["name"], "ip": h["ip"], "port": 8000, "status": "healthy",
                    "latency_ms": 70, "model": "qwen3.8-flash-next", "max_model_len": 70000}
        monkeypatch.setattr(fv, "_inventory_rows", fake_inventory)
        monkeypatch.setattr(fv, "_models_by_host", fake_models)
        monkeypatch.setattr(fv, "probe_vllm", fake_probe)
        view = fv.fleet_view()
        ips = [h["ip"] for h in view["hosts"]]
        # inventory ∪ legacy — the two inventory rows are present (the legacy
        # static list adds the rest; both stay visible per §3.4)
        assert {"10.0.20.168", "10.0.20.161"} <= set(ips)
        by_ip = {h["ip"]: h for h in view["hosts"]}
        assert by_ip["10.0.20.168"]["state"] == SERVING
        assert by_ip["10.0.20.161"]["state"] == STOPPED_EXPECTED  # rendered honestly
        assert view["states"]["SERVING"] == 1 and view["states"]["STOPPED_EXPECTED"] >= 1

    def test_legacy_list_host_without_inventory_row_visible(self, monkeypatch):
        """A host in VLLM_HOSTS but missing from the hosts table still shows."""
        monkeypatch.setattr(fv, "_inventory_rows", lambda: {})
        monkeypatch.setattr(fv, "_models_by_host", lambda: {})
        monkeypatch.setattr(fv, "probe_vllm",
                            lambda h: {"name": h["name"], "ip": h["ip"], "status": "error",
                                       "error": "down", "latency_ms": 1})
        view = fv.fleet_view()
        assert len(view["hosts"]) >= 6
        assert view["inventory_source"] == "legacy:vllm_hosts"

    def test_multiple_models_on_one_host(self, monkeypatch):
        monkeypatch.setattr(fv, "_inventory_rows", lambda: {
            "10.0.20.163": {"host_id": 3, "inventory_name": "MIAM-00143 / VM109",
                             "gpu_count": 6, "gpu_model": "RTX 3080 20GB",
                             "node": "miam00143", "vmid": 109,
                             "desired_power_state": "RUNNING",
                             "desired_service_state": "SERVING",
                             "management_mode": "MANAGED"}})
        monkeypatch.setattr(fv, "_models_by_host", lambda: {
            "10.0.20.163": [
                {"model": "gemma4-26b-a4b", "routable": True, "is_alias": False,
                 "health": "healthy", "context_limit": 70000,
                 "last_verified": datetime.now(timezone.utc).isoformat()},
                {"model": "qwen3.6-35b-a3b", "routable": False, "is_alias": False,
                 "health": "unhealthy", "context_limit": 262144, "last_verified": None},
            ]})
        monkeypatch.setattr(fv, "probe_vllm",
                            lambda h: {"name": h["name"], "ip": h["ip"], "status": "healthy",
                                       "latency_ms": 70, "model": "gemma4-26b-a4b",
                                       "max_model_len": 70000})
        view = fv.fleet_view()
        h = [x for x in view["hosts"] if x["ip"] == "10.0.20.163"][0]
        assert len(h["models"]) == 2
        assert h["state"] == SERVING  # healthy + a routable fresh model