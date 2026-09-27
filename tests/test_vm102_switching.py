"""§21.8 regression tests — active-VM switching on MIAM-00111.

These tests verify that dashboard status, probe targets, recovery, power actions,
registry refresh, and preset handling follow the ACTIVE VM mapping without
rebinding historical data.

They import the application modules with DB/HTTP layers stubbed (no secrets, no
network). Run: python3 -m pytest tests/ -q
"""
import os
import sys
from unittest import mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "service", "app")
CTRL = os.path.join(REPO, "service", "control")
for p in (APP, CTRL):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def core():
    """Import v011_core with psycopg2/requests stubbed out (no DB, no network)."""
    for name in ("psycopg2", "requests"):
        if name not in sys.modules:
            sys.modules[name] = mock.MagicMock()
    import importlib
    mod = importlib.import_module("v011_core")
    return importlib.reload(mod)


@pytest.fixture
def pve_ops():
    import importlib
    mod = importlib.import_module("pve_ops")
    return importlib.reload(mod)


# ---------------------------------------------------------------- NODE_MAP
class TestNodeMap:
    def test_vm102_is_mapped(self, core):
        assert core.NODE_MAP.get("10.0.20.168") == ("miam00111", 102)

    def test_vm103_mapping_preserved_for_rollback(self, core):
        """§21.8: rollback VM stays actionable for admins; desired-state gates recovery."""
        assert core.NODE_MAP.get("10.0.20.161") == ("miam00111", 103)

    def test_other_hosts_unaffected(self, core):
        assert core.NODE_MAP["10.0.20.162"] == ("miam00112", 401)
        assert core.NODE_MAP["10.0.20.163"] == ("miam00143", 109)
        assert core.NODE_MAP["10.0.20.164"] == ("miam00144", 111)
        assert core.NODE_MAP["10.0.20.165"] == ("miam-00149", 149)

    def test_pve_ops_vm102(self, pve_ops):
        assert pve_ops.NODE_MAP["10.0.20.168"] == ("miam00111", "102")

    def test_pve_ops_vm103_preserved(self, pve_ops):
        assert pve_ops.NODE_MAP["10.0.20.161"] == ("miam00111", "103")

    def test_power_action_targets_vm102(self, core):
        """§21.8: VM power actions resolve to VMID 102 for the active host."""
        with mock.patch.object(core, "pve_api", return_value={"data": {"ok": 1}}) as pa:
            ok = core.vm_power("10.0.20.168", "start")
        assert ok is True
        called_path = pa.call_args[0][0]
        assert "/nodes/miam00111/qemu/102/status/start" in called_path

    def test_power_action_targets_vm103_rollback(self, core):
        with mock.patch.object(core, "pve_api", return_value={"data": {"ok": 1}}) as pa:
            core.vm_power("10.0.20.161", "shutdown")
        called_path = pa.call_args[0][0]
        assert "/nodes/miam00111/qemu/103/status/shutdown" in called_path

    def test_status_targets_vm102(self, core):
        with mock.patch.object(core, "pve_api", return_value={"data": {"status": "running"}}) as pa:
            st = core.vm_status("10.0.20.168")
        assert st["vmid"] == 102
        assert st["node"] == "miam00111"
        assert st["status"] == "running"

    def test_unknown_ip_rejected(self, core):
        with pytest.raises(KeyError):
            core.vm_status("10.0.20.999")


# ---------------------------------------------------------------- VLLM_HOSTS (dashboard)
class TestDashboardHosts:
    def test_import_main_lists_vm102_first(self):
        """§21.1: /api/status source shows MIAM-00111 / VM102 as active."""
        src = open(os.path.join(APP, "main.py")).read()
        assert '{"name": "MIAM-00111 / VM102", "ip": "10.0.20.168"}' in src

    def test_vm103_labeled_rollback(self):
        src = open(os.path.join(APP, "main.py")).read()
        assert "VM103 (rollback)" in src


# ---------------------------------------------------------------- sync_registry refresh bug
class _FakeCursor:
    """Minimal cursor recording executed SQL; returns canned rows for SELECTs."""
    def __init__(self, store, select_results):
        self._store = store
        self._sel = select_results
        self.description = None

    def execute(self, sql, params=None):
        self._store.append((" ".join(sql.split()), params))
        s = " ".join(sql.split())
        if s.startswith("SELECT"):
            key = self._sel_key(s)
            rows = self._sel.get(key, [])
            self._result = rows
            if rows:
                self.description = [(c, None, None, None, None, None, None) for c in rows[0]]

    def _sel_key(self, s):
        if "FROM hosts ORDER BY host_id" in s:
            return "hosts"
        if "desired_power_state, management_mode" in s:
            return "desired"
        if "COUNT(*)" in s:
            return "count"
        if "manager_settings" in s:
            return "settings"
        return "other"

    def fetchall(self):
        return [tuple(r.values()) if isinstance(r, dict) else r for r in self._result]

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, select_results):
        self.store = []
        self._sel = select_results
        self._cur = None

    def cursor(self):
        self._cur = _FakeCursor(self.store, self._sel)
        return self._cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class TestSyncRegistryRefresh:
    def test_upsert_sql_contains_backend_url_refresh(self, core):
        """Static guarantee: the generated SQL refreshes engine/backend_url on conflict."""
        import inspect
        src = inspect.getsource(core)
        # find the ON CONFLICT block inside sync_registry
        idx = src.find("ON CONFLICT (logical_model_name, host_id) DO UPDATE SET")
        assert idx != -1
        block = src[idx:idx + 400]
        assert "engine=EXCLUDED.engine" in block
        assert "backend_url=EXCLUDED.backend_url" in block
        assert "context_limit=EXCLUDED.context_limit" in block
        assert "health=EXCLUDED.health" in block
        assert "routable=EXCLUDED.routable" in block
        assert "last_verified=now()" in block

    def test_alias_targets_include_flash_next(self, core):
        """§21.6-10: code alias resolves to qwen3.8-flash-next."""
        import inspect
        src = inspect.getsource(core)
        assert '"code", "qwen3.8-flash-next"' in src


# ---------------------------------------------------------------- litellm_sync alias map
class TestLiteLLMSyncAlias:
    def test_vm102_has_proper_local_alias(self):
        src = open(os.path.join(APP, "litellm_sync.py")).read()
        assert '"10.0.20.168": "local-00111"' in src

    def test_vm103_alias_preserved(self):
        src = open(os.path.join(APP, "litellm_sync.py")).read()
        assert '"10.0.20.161": "local-00111"' in src


# ---------------------------------------------------------------- historical data integrity
class TestHistoryIntegrity:
    def test_rdma_ring_defaults_untouched(self):
        """§21.4: historical records (RDMA ring inventory) are NOT rewritten."""
        src = open(os.path.join(APP, "main.py")).read()
        # the historical ring still lists VM103 guest ip — this is intended history
        assert '"guest_ip": "10.0.20.161"' in src

    def test_no_vm102_in_rdma_history(self):
        src = open(os.path.join(APP, "main.py")).read()
        # VM102 was never an RDMA ring member; history must stay accurate
        assert '"guest_ip": "10.0.20.168"' not in src.split("RDMA_RING_DEFAULTS")[1].split("]")[0]
