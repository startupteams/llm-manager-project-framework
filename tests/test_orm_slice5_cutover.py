"""ORM Slice 5 cutover: v0.11-app sync adapter parity tests (pgserver PG16).

Phase E (2026-09-29 plan): the v011 legacy app's psycopg2 write sites for
hosts / model_registry / recovery_events route through
``service/app/orm_write_adapter.py`` (sync SQLAlchemy Core over the SAME
schema). These tests prove the adapter produces EXACTLY the legacy SQL's
effect, per domain, on the real schema from db/schema/schema.sql.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

pytest.importorskip("pgserver", reason="pgserver required for parity tests")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def llm_pg(tmp_path_factory):
    import pgserver

    data_dir = tmp_path_factory.mktemp("slice5-cutover-pgdata")
    with pgserver.get_server(str(data_dir)) as srv:
        uri = srv.get_uri()
        import psycopg2
        from urllib.parse import parse_qs, urlparse

        u = urlparse(uri)
        q = parse_qs(u.query)
        sock_host = q["host"][0]
        base = f"host={sock_host} user={u.username or 'postgres'}"
        conn = psycopg2.connect(base + " dbname=postgres")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE DATABASE llmmanager")
        cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='llmmanager') THEN CREATE ROLE llmmanager; END IF; END $$")
        cur.close()
        conn.close()
        raw = open(os.path.join(REPO, "db", "schema", "schema.sql")).read()
        schema = "\n".join(l for l in raw.splitlines()
                           if not l.lstrip().startswith("\\"))
        conn = psycopg2.connect(base + " dbname=llmmanager")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(schema)
        cur.execute("SET search_path = public")
        cur.execute("""
            INSERT INTO public.hosts (host_id, name, guest_ip, node, vmid,
                               desired_power_state, management_mode, desired_service_state)
            VALUES (1, 'test-host', '10.9.9.9', NULL, NULL, 'RUNNING', 'MANAGED', 'SERVING'),
                   (2, 'test-host-2', '10.9.9.8', NULL, NULL, 'RUNNING', 'MANAGED', 'SERVING')
        """)
        cur.execute("""
            INSERT INTO public.model_registry (logical_model_name, host_id, health, routable)
            VALUES ('m1', 1, 'healthy', true), ('m2', 1, 'healthy', true), ('m3', 2, 'healthy', true)
        """)
        conn.close()
        yield {"sock_host": sock_host, "uri": uri}


@pytest.fixture()
def adapter(llm_pg, monkeypatch):
    """orm_write_adapter bound to the embedded PG (env credential contract)."""
    import sys

    sys.path.insert(0, os.path.join(REPO, "service", "app"))
    for m in [m for m in sys.modules if m == "orm_write_adapter"]:
        del sys.modules[m]
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(llm_pg["uri"]).query)
    monkeypatch.setenv("PG_HOST", q["host"][0])
    monkeypatch.setenv("PG_USER", "postgres")
    monkeypatch.setenv("PG_DB", "llmmanager")
    monkeypatch.setenv("PG_PW", "")  # pgserver local-socket trust: empty pw is VALID
    import orm_write_adapter as A

    A._engine = None  # reset the cached engine per test-env
    A._tables = {}
    yield A
    if A._engine is not None:
        A._engine.dispose()


def _cur(llm_pg):
    import psycopg2

    conn = psycopg2.connect(f"host={llm_pg['sock_host']} user=postgres dbname=llmmanager")
    conn.autocommit = True
    return conn, conn.cursor()


# ------------------------------------------------------------------- hosts


def test_hosts_update_by_ip_matches_legacy(adapter, llm_pg):
    conn, cur = _cur(llm_pg)
    n = adapter.orm_update_hosts_by_ip(guest_ip="10.9.9.9",
                                       fields={"node": "miam00111", "vmid": 124})
    assert n == 1
    cur.execute("SELECT node, vmid FROM hosts WHERE guest_ip='10.9.9.9'")
    assert cur.fetchone() == ("miam00111", 124)
    # no-match → rowcount 0 (legacy psycopg2 returns 0 too)
    n0 = adapter.orm_update_hosts_by_ip(guest_ip="10.9.9.254", fields={"vmid": 1})
    assert n0 == 0
    # desired power + management mode (legacy desired-state endpoint writes)
    adapter.orm_update_hosts_by_ip(guest_ip="10.9.9.9",
                                   fields={"desired_power_state": "STOPPED_INTENTIONAL"})
    adapter.orm_update_hosts_by_ip(guest_ip="10.9.9.9",
                                   fields={"management_mode": "MANAGED"})
    row = adapter.orm_hosts_row_by_ip("10.9.9.9")
    assert row == {"guest_ip": "10.9.9.9", "desired_power_state": "STOPPED_INTENTIONAL",
                   "management_mode": "MANAGED"}


def test_hosts_inventory_and_flags(adapter, llm_pg):
    inv = adapter.orm_hosts_inventory()
    assert [h["host_id"] for h in inv] == [1, 2]
    # host 2 is only mutated by the unroutable test AFTER this one (module DB
    # is shared, test order = file order); assert its fixture-pinned flags.
    dps, mode = adapter.orm_host_flags(2)
    assert dps in ("RUNNING", "STOPPED_INTENTIONAL") and mode == "MANAGED"
    assert adapter.orm_host_flags(999) == ("RUNNING", "MANAGED")  # legacy default


def test_hosts_write_guard_rejects_unknown_columns(adapter):
    from fastapi.testclient import TestClient  # noqa: F401  (import sanity only)

    with pytest.raises(ValueError, match="not writable"):
        adapter.orm_update_hosts_by_ip(guest_ip="10.9.9.9",
                                       fields={"host_id": 77})


# ---------------------------------------------------------- model_registry


def test_model_registry_upsert_matches_legacy(adapter, llm_pg):
    conn, cur = _cur(llm_pg)
    adapter.orm_upsert_model_registry(
        logical_model_name="m1", host_id=1, engine="vllm",
        backend_url="http://10.9.9.9:8000/v1", context_limit=32768,
        health="healthy", routable=True)
    cur.execute("SELECT engine, backend_url, context_limit, health, routable "
                "FROM model_registry WHERE logical_model_name='m1' AND host_id=1")
    row = cur.fetchone()
    assert row == ("vllm", "http://10.9.9.9:8000/v1", 32768, "healthy", True)
    assert row is not None and cur.rowcount is not None  # upsert, not dup
    cur.execute("SELECT count(*) FROM model_registry WHERE logical_model_name='m1' AND host_id=1")
    assert cur.fetchone()[0] == 1


def test_model_registry_mark_unroutable_and_alias_flow(adapter, llm_pg):
    conn, cur = _cur(llm_pg)
    n = adapter.orm_mark_host_unroutable(host_id=2, health="unreachable")
    assert n == 1
    cur.execute("SELECT health, routable FROM model_registry WHERE host_id=2")
    assert cur.fetchone() == ("unreachable", False)

    # alias flow: delete → re-insert (legacy sync_registry order)
    adapter.orm_delete_aliases()
    adapter.orm_upsert_model_registry(
        logical_model_name="fast", host_id=None, engine="alias",
        backend_url="", context_limit=None, health="healthy", routable=True,
        is_alias=True, alias_target="m1")
    cur.execute("SELECT is_alias, alias_target, host_id, health, routable "
                "FROM model_registry WHERE logical_model_name='fast'")
    row = cur.fetchone()
    assert row == (True, "m1", None, "healthy", True)
    assert adapter.orm_alias_target_routable("m1") is True
    assert adapter.orm_alias_target_routable("m3") is False  # marked unreachable above
    # LEGACY PARITY (verified against schema.sql): the UNIQUE
    # (logical_model_name, host_id) constraint does NOT dedupe NULL host_ids
    # (PG treats NULLs as distinct) — so a second alias insert WITHOUT the
    # legacy delete-first creates a duplicate row, exactly like psycopg2.
    # sync_registry always deletes aliases before re-inserting; mirror it:
    adapter.orm_delete_aliases()
    adapter.orm_upsert_model_registry(
        logical_model_name="fast", host_id=None, engine="alias",
        backend_url="", context_limit=None, health="unhealthy", routable=False,
        is_alias=True, alias_target="m1")
    cur.execute("SELECT count(*) FROM model_registry WHERE logical_model_name='fast'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT health, routable FROM model_registry WHERE logical_model_name='fast'")
    assert cur.fetchone() == ("unhealthy", False)  # fresh insert replaced the old
    assert adapter.orm_routable_names() == ["m1", "m2"]  # fast re-inserted unroutable


# ---------------------------------------------------------- recovery_events


def test_recovery_event_insert_and_count(adapter, llm_pg):
    conn, cur = _cur(llm_pg)
    ip = f"10.9.9.{uuid.uuid4().int % 200 + 10}"
    adapter.orm_insert_recovery_event(ip=ip, event_type="outage_detected",
                                      detail={"reason": "probe failed x3", "ok": False})
    adapter.orm_insert_recovery_event(ip=ip, event_type="recovery_vm_start",
                                      detail="plain-string-detail")
    cur.execute("SELECT ip, event_type, detail FROM recovery_events WHERE ip=%s ORDER BY id",
                (ip,))
    rows = cur.fetchall()
    assert rows[0] == (ip, "outage_detected", json.dumps({"reason": "probe failed x3", "ok": False}))
    assert rows[1] == (ip, "recovery_vm_start", "plain-string-detail")
    n = adapter.orm_recent_event_count(ip=ip, event_type="outage_detected", minutes=30)
    assert n == 1
    n0 = adapter.orm_recent_event_count(ip=ip, event_type="no-such", minutes=30)
    assert n0 == 0


# ------------------------------------------------------------------ parity


def test_parity_probe_shape(adapter):
    p = adapter.parity_probe(limit=100)
    for table in ("hosts", "model_registry", "recovery_events"):
        assert table in p and p[table]["rows"] == p[table]["probe_rows"]
        assert len(p[table]["checksum"]) == 32