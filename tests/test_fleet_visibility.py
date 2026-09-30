"""2026-09-30 fleet-invisibility regression tests (window-5 plan §2).

Bug: `_physical_host_for_ip` led with `ip.startswith("10.0.20.16")`, which
matches EVERY guest in the .161-.165 range, collapsing all six hosts onto the
MIAM-00111 physical label. `_active_hosts()` then promoted only the first DB
match (VM102) and demoted nothing else onto other physical hosts — the
dashboard rendered exactly ONE inference-host card while five hosts were
serving. Verified live on prod (ae732e2): `_active_hosts()` returned
1 host; simulating the fixed matcher returns 6 (5 healthy + 1 stopped).

Run: python3 -m pytest tests/test_fleet_visibility.py -q
No DB required — the matcher and _active_hosts() are pure given a stubbed
active_deployments module.
"""
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "service", "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

pytest.importorskip("ldap3")
pytest.importorskip("fastapi")


@pytest.fixture(scope="module")
def app_main():
    """Import service/app/main.py without running the server."""
    import main as llmmain  # noqa: E402

    return llmmain


class TestPhysicalHostForIp:
    """Exact-IP mapping — the .16x prefix must never swallow sibling hosts."""

    def test_vm102_and_vm103_map_to_miam00111(self, app_main):
        assert app_main._physical_host_for_ip("10.0.20.168") == "MIAM-00111"
        assert app_main._physical_host_for_ip("10.0.20.161") == "MIAM-00111"

    def test_vm401_maps_to_miam00112(self, app_main):
        assert app_main._physical_host_for_ip("10.0.20.162") == "MIAM-00112"

    def test_vm109_maps_to_miam00143(self, app_main):
        assert app_main._physical_host_for_ip("10.0.20.163") == "MIAM-00143"

    def test_vm111_maps_to_miam00144(self, app_main):
        assert app_main._physical_host_for_ip("10.0.20.164") == "MIAM-00144"

    def test_vm149_maps_to_miam00149(self, app_main):
        assert app_main._physical_host_for_ip("10.0.20.165") == "MIAM-00149"

    def test_every_inventory_host_resolves_to_its_own_label(self, app_main):
        """The actual bug: all six inventory IPs collapsed onto MIAM-00111."""
        phys = {h["ip"]: app_main._physical_host_for_ip(h["ip"]) for h in app_main.VLLM_HOSTS}
        values = set(phys.values())
        assert len(values) == 5, f"expected 5 distinct physical hosts, got {values}"

    def test_unknown_ip_returns_ip_as_label(self, app_main):
        assert app_main._physical_host_for_ip("10.0.20.250") == "10.0.20.250"


def _stub_ad(mapping):
    """Stub the active_deployments module interface used by _active_hosts()."""
    return types.SimpleNamespace(active_ip_for=lambda phys, fallback=None: mapping.get(phys, fallback))


class TestActiveHostsVisibility:
    def test_all_hosts_visible_with_full_active_map(self, app_main, monkeypatch):
        mapping = {
            "MIAM-00111": "10.0.20.168",
            "MIAM-00112": "10.0.20.162",
            "MIAM-00143": "10.0.20.163",
            "MIAM-00144": "10.0.20.164",
            "MIAM-00149": "10.0.20.165",
        }
        monkeypatch.setattr(app_main, "_AD_ENABLED", True)
        monkeypatch.setattr(app_main, "_ad", _stub_ad(mapping))
        hosts = app_main._active_hosts()
        assert len(hosts) == 6
        ips = [h["ip"] for h in hosts]
        assert set(ips) == {h["ip"] for h in app_main.VLLM_HOSTS}

    def test_ad_disabled_returns_full_inventory(self, app_main, monkeypatch):
        monkeypatch.setattr(app_main, "_AD_ENABLED", False)
        hosts = app_main._active_hosts()
        assert len(hosts) == 6

    def test_no_active_entry_still_lists_host(self, app_main, monkeypatch):
        """Powered-off/never-promoted host stays visible (plan §3.4)."""
        monkeypatch.setattr(app_main, "_AD_ENABLED", True)
        monkeypatch.setattr(app_main, "_ad", _stub_ad({}))
        hosts = app_main._active_hosts()
        assert len(hosts) == 6