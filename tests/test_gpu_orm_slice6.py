"""ORM Slice 6: gpu_samples family ORM access tests (pgserver PG16).

Phase F (2026-09-29 plan): the retention architecture is live (raw 90d /
hourly 730d), so gpu_samples ORM access may proceed. Rules honored:
- bulk ingestion (never row-at-a-time ORM objects)
- rollup/prune SQL semantics preserved EXACTLY (hour-aligned, upsert refresh,
  aggregate-before-prune, bounded batches)
- no schema change, no new indexes; LiteLLM-owned tables untouched
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("pgserver", reason="pgserver required for slice 6 tests")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def llm_pg(tmp_path_factory):
    """Embedded PG16 with schema.sql + the 002 retention migration applied."""
    import psycopg2
    import pgserver

    data_dir = tmp_path_factory.mktemp("slice6-gpu-pgdata")
    with pgserver.get_server(str(data_dir)) as srv:
        uri = srv.get_uri()
        from urllib.parse import parse_qs, urlparse

        u = urlparse(uri)
        q = parse_qs(u.query)
        sock_host = q["host"][0]
        base = f"host={sock_host} user={u.username or 'postgres'}"
        conn = psycopg2.connect(base + " dbname=postgres")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE DATABASE llmmanager")
        cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='llmmanager') "
                    "THEN CREATE ROLE llmmanager; END IF; END $$")
        cur.close()
        conn.close()
        raw = open(os.path.join(REPO, "db", "schema", "schema.sql")).read()
        schema = "\n".join(l for l in raw.splitlines() if not l.lstrip().startswith("\\"))
        mig = open(os.path.join(REPO, "db", "migrations", "002_gpu_samples_hourly_retention.sql")).read()
        conn = psycopg2.connect(base + " dbname=llmmanager")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(schema)
        cur.execute("SET search_path = public")
        cur.execute(mig)
        # fixture host for FK-less gpu rows (gpu_samples has no FK; host_id int)
        cur.execute("INSERT INTO hosts (host_id, name, guest_ip, desired_power_state, "
                    "management_mode, desired_service_state) "
                    "VALUES (7, 'gpu-test', '10.7.7.7', 'RUNNING', 'MANAGED', 'SERVING')")
        conn.close()
        yield {"sock_host": sock_host, "uri": uri}


@pytest.fixture()
def gpu(llm_pg, monkeypatch):
    import sys

    sys.path.insert(0, os.path.join(REPO, "service", "app"))
    for m in [m for m in sys.modules if m == "gpu_orm"]:
        del sys.modules[m]
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(llm_pg["uri"]).query)
    monkeypatch.setenv("PG_HOST", q["host"][0])
    monkeypatch.setenv("PG_USER", "postgres")
    monkeypatch.setenv("PG_DB", "llmmanager")
    monkeypatch.setenv("PG_PW", "")
    import gpu_orm as G

    G._engine = None
    G._tables = {}
    yield G
    if G._engine is not None:
        G._engine.dispose()


def _cur(llm_pg):
    import psycopg2

    conn = psycopg2.connect(f"host={llm_pg['sock_host']} user=postgres dbname=llmmanager")
    conn.autocommit = True
    return conn, conn.cursor()


def test_bulk_insert_samples(gpu, llm_pg):
    conn, cur = _cur(llm_pg)
    ts = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    rows = [(7, 0, 41.0, 8192.0, 180.0), (7, 1, 77.0, 12288.0, 240.0)]
    n = gpu.insert_samples_bulk(rows, ts=ts)
    assert n == 2
    cur.execute("SELECT host_id, gpu_index, util_pct, mem_used_mib, power_watts "
                "FROM gpu_samples WHERE host_id=7 ORDER BY gpu_index")
    got = cur.fetchall()
    assert got == [(7, 0, 41.0, 8192.0, 180.0), (7, 1, 77.0, 12288.0, 240.0)]
    assert gpu.insert_samples_bulk([]) == 0  # empty cycle is a no-op


def test_hourly_rollup_upsert_matches_retention_semantics(gpu, llm_pg):
    conn, cur = _cur(llm_pg)
    hour0 = (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
             - timedelta(hours=1))
    # seed a closed hour: 4 samples, 2 GPUs
    cur.execute("""INSERT INTO gpu_samples (ts, host_id, gpu_index, util_pct, mem_used_mib, power_watts)
        VALUES (%s,7,0,10.0,1000.0,100.0), (%s,7,0,30.0,2000.0,300.0),
               (%s,7,1,50.0,4000.0,200.0), (%s,7,1,90.0,8000.0,400.0)""",
                (hour0, hour0 + timedelta(minutes=15),
                 hour0 + timedelta(minutes=30), hour0 + timedelta(minutes=45)))
    n = gpu.hourly_rollup_window(hour0, hour0 + timedelta(hours=1))
    assert n >= 2  # one bucket per (gpu) → 2 buckets
    cur.execute("SELECT sample_count, util_avg, power_avg_w, power_sum_wh, last_ts "
                "FROM gpu_samples_hourly WHERE host_id=7 AND gpu_index=0")
    row = cur.fetchone()
    assert row[0] == 2
    assert abs(float(row[1]) - 20.0) < 1e-6      # avg(10, 30) (numeric→Decimal)
    assert abs(float(row[2]) - 200.0) < 1e-6     # avg(100, 300)
    assert abs(float(row[3]) - 400.0) < 1e-6     # sum watts
    # UPSERT refresh (W2 lesson): re-run with same window → same values, no dup rows
    n2 = gpu.hourly_rollup_window(hour0, hour0 + timedelta(hours=1))
    cur.execute("SELECT count(*) FROM gpu_samples_hourly WHERE host_id=7 AND gpu_index=0")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT util_avg FROM gpu_samples_hourly WHERE host_id=7 AND gpu_index=0")
    assert abs(float(cur.fetchone()[0]) - 20.0) < 1e-6
    # alignment: bucket is hour-aligned
    cur.execute("SELECT hour_bucket = date_trunc('hour', hour_bucket) FROM gpu_samples_hourly LIMIT 1")
    assert cur.fetchone()[0] is True


def test_prune_bounded_batches_and_keeps_aggregates(gpu, llm_pg):
    conn, cur = _cur(llm_pg)
    cutoff = datetime.now(timezone.utc) - timedelta(days=89)
    # raw rows inside retention survive
    cur.execute("SELECT count(*) FROM gpu_samples WHERE host_id=7")
    before = cur.fetchone()[0]
    deleted = gpu.prune_raw_older_than(cutoff, batch=10)
    assert deleted == 0  # nothing older than 89d in the fixture
    cur.execute("SELECT count(*) FROM gpu_samples WHERE host_id=7")
    assert cur.fetchone()[0] == before
    cur.execute("SELECT count(*) FROM gpu_samples_hourly WHERE host_id=7")
    assert cur.fetchone()[0] == 2  # aggregates NEVER touched by prune


def test_window_reads(gpu, llm_pg):
    conn, cur = _cur(llm_pg)
    hour0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    rows = gpu.fetch_recent_window(since=hour0 - timedelta(minutes=5), host_id=7)
    # 4 seeded rollup-test rows in the closed hour + 2 bulk-insert rows (same module DB)
    assert len(rows) == 6
    hs = gpu.hourly_window(since=hour0 - timedelta(hours=1), until=hour0 + timedelta(hours=1), host=7)
    assert len(hs) == 2
    # settings read (config-backed retention keys)
    cur.execute("INSERT INTO manager_settings (key, value, updated_at, updated_by) "
                "VALUES ('gpu_samples_retention_days', '90', now(), 'test') "
                "ON CONFLICT (key) DO UPDATE SET value='90'")
    s = gpu.retention_settings()
    assert s.get("gpu_samples_retention_days") == "90"


def test_parity_probe_shape(gpu):
    p = gpu.parity_probe(limit=100)
    for table in ("gpu_samples", "gpu_samples_hourly", "host_power_rollup_1m"):
        assert table in p
        assert p[table]["rows"] == p[table]["probe_rows"]
        assert len(p[table]["checksum"]) == 32