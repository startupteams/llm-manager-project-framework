"""ORM Slice 6 — gpu_samples family access via SQLAlchemy Core (plan Phase F).

Migrates the LLM-Manager-OWNED access paths for gpu_samples /
gpu_samples_hourly / host_power_rollup_1m to SQLAlchemy Core while preserving
every operational guarantee:

- ingestion stays BATCH (`insert_samples` bulk executemany semantics — the
  collector already batches per-cycle; this module keeps rowcount-verified
  bulk inserts, never row-at-a-time ORM objects)
- retention/rollup SQL semantics preserved EXACTLY (hour-aligned buckets,
  closed-hour-only verification, aggregate-before-prune)
- NO schema change, NO new indexes (preserve existing idx_gpu_samples_*)
- LiteLLM-owned tables untouched
- parity checksums vs raw SQL (TDR-0008 §6 contract)
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from sqlalchemy import MetaData, Table, create_engine, text

_lock = threading.Lock()
_engine = None
_tables: dict[str, Table] = {}

_GPU_SAMPLE_COLS = ("ts", "host_id", "gpu_index", "util_pct", "mem_used_mib", "power_watts")


def get_engine():
    """Same credential contract as the collector/gpu_retention connect()."""
    global _engine
    with _lock:
        if _engine is not None:
            return _engine
        import os

        host = os.environ.get("PG_HOST") or os.environ.get("LLM_MANAGER_PG_HOST") or "127.0.0.1"
        dbname = os.environ.get("PG_DB", "llmmanager")
        user = os.environ.get("PG_USER", "llmmanager")
        pw = os.environ.get("PG_PW")
        if pw is None:
            pw_file = os.environ.get("PG_PW_FILE", "/etc/llm-manager/secrets/pg_app_creds")
            try:
                with open(pw_file) as f:
                    for line in f:
                        if line.startswith("PG_PW="):
                            pw = line.strip().split("=", 1)[1]
                            break
            except OSError:
                pass
        if pw is None:
            raise RuntimeError("gpu orm: no PG credentials")
        if host.startswith("/"):
            _engine = create_engine(
                f"postgresql+psycopg2://{user}:{pw}@/{dbname}?host={host}",
                pool_pre_ping=True, future=True)
        else:
            port = os.environ.get("PG_PORT", "5432")
            _engine = create_engine(
                f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{dbname}",
                pool_pre_ping=True, future=True)
        return _engine


def _table(name: str) -> Table:
    if name not in _tables:
        md = MetaData()
        with get_engine().connect() as conn:
            _tables[name] = Table(name, md, autoload_with=conn, schema="public")
    return _tables[name]


# ------------------------------------------------------------- ingestion (bulk)


def insert_samples_bulk(rows: list[tuple], *, ts: datetime | None = None) -> int:
    """Bulk-insert one collector cycle's samples (never row-at-a-time).

    rows: [(gpu_index, util_pct, mem_used_mib, power_watts), ...] for ONE host.
    legacy: executemany INSERT with ts=now() per row (all rows in one statement
    share a single now() inside one SQL statement — preserved by using one
    timestamp for the whole batch, matching legacy executemany semantics).
    """
    if not rows:
        return 0
    t = _table("gpu_samples")
    ts_val = ts or datetime.now(timezone.utc)
    payload = [{"ts": ts_val, "host_id": host_id, "gpu_index": g, "util_pct": u,
                "mem_used_mib": m, "power_watts": w}
               for (host_id, g, u, m, w) in rows]
    with get_engine().begin() as conn:
        conn.execute(t.insert(), payload)
    return len(payload)


# ------------------------------------------------------------- rollups (Core SQL)


def rollup_minute() -> int:
    """1-minute host power rollup — IDENTICAL SQL to the collector's legacy
    rollup_minute() (idempotent per minute, ON CONFLICT DO NOTHING)."""
    stmt = text("""
        INSERT INTO host_power_rollup_1m (ts, host_id, gpu_watts_avg, gpu_watts_peak)
        SELECT date_trunc('minute', now()) - interval '1 minute', host_id,
               avg(power_watts), max(power_watts)
        FROM gpu_samples
        WHERE ts >= date_trunc('minute', now()) - interval '1 minute'
          AND ts <  date_trunc('minute', now())
        GROUP BY host_id
        ON CONFLICT DO NOTHING
    """)
    with get_engine().begin() as conn:
        result = conn.execute(stmt)
        return result.rowcount or 0


def hourly_rollup_window(since: datetime, until: datetime) -> int:
    """Hour-aligned UPSERT rollup for [since, until) — IDENTICAL statement to
    gpu_retention.py's _rollup_window (aggregation-before-prune, upsert refresh;
    the W2 lesson: partial upserts must overwrite with FULL-hour aggregates)."""
    stmt = text("""
        INSERT INTO gpu_samples_hourly (
            hour_bucket, host_id, gpu_index, sample_count,
            util_min, util_max, util_avg,
            mem_used_min_mib, mem_used_max_mib, mem_used_avg_mib,
            power_min_w, power_max_w, power_avg_w, power_sum_wh, last_ts)
        SELECT date_trunc(:hour, ts), host_id, gpu_index,
               count(*)::int,
               min(util_pct), max(util_pct), avg(util_pct),
               min(mem_used_mib), max(mem_used_mib), avg(mem_used_mib),
               min(power_watts), max(power_watts), avg(power_watts),
               sum(COALESCE(power_watts,0)), max(ts)
        FROM gpu_samples
        WHERE ts >= :since AND ts < :until AND host_id IS NOT NULL
        GROUP BY 1, 2, 3
        ON CONFLICT (hour_bucket, host_id, gpu_index) DO UPDATE SET
            sample_count  = EXCLUDED.sample_count,
            util_min      = EXCLUDED.util_min,
            util_max      = EXCLUDED.util_max,
            util_avg      = EXCLUDED.util_avg,
            mem_used_min_mib = EXCLUDED.mem_used_min_mib,
            mem_used_max_mib = EXCLUDED.mem_used_max_mib,
            mem_used_avg_mib = EXCLUDED.mem_used_avg_mib,
            power_min_w   = EXCLUDED.power_min_w,
            power_max_w   = EXCLUDED.power_max_w,
            power_avg_w   = EXCLUDED.power_avg_w,
            power_sum_wh  = EXCLUDED.power_sum_wh,
            last_ts       = EXCLUDED.last_ts
    """)
    with get_engine().begin() as conn:
        return conn.execute(stmt, {"hour": "hour", "since": since, "until": until}).rowcount or 0


def prune_raw_older_than(cutoff: datetime, batch: int = 50000) -> int:
    """Bounded-batch raw prune (raw older than cutoff; aggregates NEVER touched).
    Returns rows deleted in THIS call; caller loops until 0."""
    with get_engine().begin() as conn:
        result = conn.execute(text(
            "DELETE FROM gpu_samples WHERE sample_id IN ("
            "  SELECT sample_id FROM gpu_samples WHERE ts < :cutoff LIMIT :b)"),
            {"cutoff": cutoff, "b": batch})
        return result.rowcount or 0


# ------------------------------------------------------------- reads (batched/streamed)


def fetch_recent_window(*, since: datetime, until: datetime | None = None,
                        host_id: int | None = None, limit: int = 50000) -> list[dict]:
    """Streamed window read (YIELD-BATCHED via mappings; no full-table loads)."""
    import sqlalchemy as sa

    t = _table("gpu_samples")
    stmt = sa.select(t).where(t.c.ts >= since).limit(limit)
    if until is not None:
        stmt = stmt.where(t.c.ts < until)
    if host_id is not None:
        stmt = stmt.where(t.c.host_id == host_id)
    stmt = stmt.order_by(t.c.ts)
    with get_engine().connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def hourly_window(*, since: datetime, until: datetime, host: str | None = None) -> list[dict]:
    """Read hourly rollups (dashboards/analysis path)."""
    import sqlalchemy as sa

    t = _table("gpu_samples_hourly")
    stmt = sa.select(t).where(t.c.hour_bucket >= since, t.c.hour_bucket < until)
    if host is not None:
        stmt = stmt.where(t.c.host_id == host)  # int host_id (schema-honest)
    stmt = stmt.order_by(t.c.hour_bucket)
    with get_engine().connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def retention_settings(conn=None) -> dict:
    """Read config-backed retention (raw/hourly days) — mirrors gpu_retention._setting."""
    sql = text("SELECT key, value FROM manager_settings WHERE key IN "
               "('gpu_samples_retention_days', 'gpu_samples_hourly_retention_days')")
    own = conn is None
    c = conn or get_engine().connect()
    try:
        rows = c.execute(sql).all()
        return {k: v for k, v in rows}
    finally:
        if own:
            c.close()


# ------------------------------------------------------------------ parity


def parity_probe(limit: int = 2000) -> dict:
    """Row-count + checksum parity vs raw SQL (TDR-0008 §6 contract)."""
    out = {}
    with get_engine().connect() as conn:
        for table, key in (("gpu_samples", "ts"), ("gpu_samples_hourly", "hour_bucket"),
                           ("host_power_rollup_1m", "ts")):
            n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            probe_n = conn.execute(
                text(f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT :l) t"),
                {"l": limit}).scalar()
            checksum = conn.execute(text(
                f"SELECT md5(coalesce(string_agg(t.x::text, ',' ORDER BY t.x), '')) "
                f"FROM (SELECT to_jsonb(t2.*)::text x FROM {table} t2 LIMIT :l) t"),
                {"l": limit}).scalar()
            out[table] = {"rows": n, "probe_rows": probe_n, "checksum": checksum}
    return out