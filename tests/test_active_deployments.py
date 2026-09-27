"""§21.7/§21.8 regression tests — DB-backed active deployment switching.

Simulates: MIAM-00111 active = VM103/.161 -> VM102/.168 -> back to VM103/.161,
verifying consumers follow the DB source of truth without Python edits, and that
historical data stays untouched.

Uses pgserver (embedded PostgreSQL) — same no-Docker validation path as ACMS.
Run: python3 -m pytest tests/test_active_deployments.py -q
"""
import os
import sys
import time
from unittest import mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "service", "app")
for p in (APP,):
    if p not in sys.path:
        sys.path.insert(0, p)

pgserver = pytest.importorskip("pgserver", reason="pgserver required for DB-backed tests")


@pytest.fixture(scope="module")
def pg_dsn():
    import tempfile
    with pgserver.get_server(tempfile.mkdtemp(prefix="pgtest-ad-")) as srv:
        os.environ["LLM_MANAGER_PG_DSN"] = srv.get_uri()
        yield srv.get_uri()
        os.environ.pop("LLM_MANAGER_PG_DSN", None)


@pytest.fixture
def ad(pg_dsn):
    import active_deployments
    import importlib
    mod = importlib.reload(active_deployments)
    assert mod.ensure_table()
    return mod


@pytest.fixture
def core(pg_dsn):
    for name in ("psycopg2", "requests"):
        if name not in sys.modules or getattr(sys.modules[name], "__wrapped__", None) is None:
            pass
    import importlib
    mod = importlib.import_module("v011_core")
    return importlib.reload(mod)


class TestActiveDeploymentSwitching:
    def test_table_created(self, ad):
        import psycopg2
        with psycopg2.connect(os.environ["LLM_MANAGER_PG_DSN"]) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.active_hosts')")
                assert cur.fetchone()[0] == "active_hosts"

    def test_initially_no_active(self, ad):
        assert ad.all_active() == {}
        assert ad.active_ip_for("MIAM-00111", fallback="legacy-ip") == "legacy-ip"

    def test_promote_vm102(self, ad):
        r = ad.set_active("MIAM-00111", "10.0.20.168", updated_by="test")
        assert r == {"status": "ok", "physical_host": "MIAM-00111", "active_host_ip": "10.0.20.168"}
        assert ad.active_ip_for("MIAM-00111", fallback="legacy") == "10.0.20.168"

    def test_switch_102_then_back_103_without_python_edit(self, ad):
        """§21.8 core scenario: VM102 -> VM103 rollback via DB only."""
        ad.set_active("MIAM-00111", "10.0.20.168")
        assert ad.active_ip_for("MIAM-00111") == "10.0.20.168"
        ad.set_active("MIAM-00111", "10.0.20.161")
        assert ad.active_ip_for("MIAM-00111") == "10.0.20.161"
        ad.set_active("MIAM-00111", "10.0.20.168")
        assert ad.active_ip_for("MIAM-00111") == "10.0.20.168"

    def test_other_physical_hosts_independent(self, ad):
        ad.set_active("MIAM-00111", "10.0.20.168")
        ad.set_active("MIAM-00112", "10.0.20.162")
        assert ad.active_ip_for("MIAM-00111") == "10.0.20.168"
        assert ad.active_ip_for("MIAM-00112") == "10.0.20.162"

    def test_cache_expiry_refreshes(self, ad):
        ad.set_active("MIAM-00111", "10.0.20.168")
        assert ad.active_ip_for("MIAM-00111") == "10.0.20.168"
        ad.set_active("MIAM-00111", "10.0.20.161")
        # cache is 30s TTL; force expiry by monkeypatching timestamps
        ad._CACHE.clear()
        assert ad.active_ip_for("MIAM-00111") == "10.0.20.161"

    def test_updated_at_and_by_recorded(self, ad, pg_dsn):
        import psycopg2
        ad.set_active("MIAM-00144", "10.0.20.164", updated_by="jordan")
        with psycopg2.connect(pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT updated_by FROM active_hosts WHERE physical_host=%s", ("MIAM-00144",))
                assert cur.fetchone()[0] == "jordan"


class TestStatusFollowsActive:
    def test_main_module_sorts_active_first(self, pg_dsn, ad):
        """§21.8: /api/status follows the active VM (active host sorts first)."""
        import main as m
        import importlib
        m = importlib.reload(m)
        ad.set_active("MIAM-00111", "10.0.20.168")
        eff = m._active_hosts()
        assert eff[0]["ip"] == "10.0.20.168", f"VM102 should sort first, got {[h['ip'] for h in eff]}"
        assert m._is_active_ip("10.0.20.168") is True
        assert m._is_active_ip("10.0.20.161") is False

    def test_rollback_switch_flips_status_order(self, pg_dsn, ad):
        import main as m
        ad._CACHE.clear()
        ad.set_active("MIAM-00111", "10.0.20.161")
        m._CACHE_like = None  # no-op; _active_hosts reads via ad cache
        eff = m._active_hosts()
        assert eff[0]["ip"] == "10.0.20.161"
        # restore prod reality
        ad._CACHE.clear()
        ad.set_active("MIAM-00111", "10.0.20.168")

    def test_legacy_fallback_when_db_down(self, ad, pg_dsn):
        """§21.7 fallback: DB unavailable -> legacy constants behavior, no crash."""
        with mock.patch.object(ad, "_pg", return_value=None):
            assert ad.active_ip_for("MIAM-00111", fallback="10.0.20.168") == "10.0.20.168"


class TestHistoryIntegrity:
    def test_switching_does_not_touch_deployment_history_tables(self, ad, pg_dsn):
        import psycopg2
        with psycopg2.connect(pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS deployment_revisions (revision_id INT, vm TEXT)")
                cur.execute("INSERT INTO deployment_revisions VALUES (17, 'VM103')")
        ad.set_active("MIAM-00111", "10.0.20.168")
        ad.set_active("MIAM-00111", "10.0.20.161")
        with psycopg2.connect(pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT vm FROM deployment_revisions WHERE revision_id=17")
                assert cur.fetchone()[0] == "VM103", "historical revisions must remain immutable"
