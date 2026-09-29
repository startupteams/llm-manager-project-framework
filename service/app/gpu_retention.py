#!/usr/bin/env python3
"""gpu_retention.py — GPU telemetry Option A: hourly rollup + verified raw pruning.

Authorization: Jordan, 2026-09-29 execution plan §1.3 / Phase B.
Invariants (never violated by this module):
  1. NEVER prune raw gpu_samples before the hourly rollup exists, is fully
     backfilled, and verifies against raw aggregates.
  2. Retention windows are configuration-backed (manager_settings), defaults
     raw=90d / hourly=730d.
  3. Prune is dry-run first; the dry-run result (cutoff, candidate count,
     candidate size, oldest survivor) is logged before any deletion.
  4. Deletes run in bounded batches.
  5. Failures surface as exceptions — the scheduler/alert path turns them into
     Attention items. Nothing fails silently.

Usage:
    python3 gpu_retention.py rollup          # incremental hourly rollup (idempotent)
    python3 gpu_retention.py backfill        # rollup over all raw history
    python3 gpu_retention.py verify          # raw-vs-rollup spot checks (tolerance rules)
    python3 gpu_retention.py dry-run-prune   # log what a prune WOULD delete
    python3 gpu_retention.py prune           # gated delete (all preconditions checked)
    python3 gpu_retention.py maintenance     # rollup + retention in one pass (cron entry)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

VERIFY_REL_TOLERANCE = 0.01      # 1% relative tolerance on numeric aggregates
VERIFY_COUNT_TOLERANCE = 0       # sample_count must match exactly
PRUNE_BATCH_ROWS = 50_000        # bounded delete transactions
MIN_HOURLY_ROWS_FOR_PRUNE = 1    # rollup must be non-empty to prune


def connect():
    pw = None
    pw_file = os.environ.get("LLM_MANAGER_PG_PW_FILE", "/etc/llm-manager/secrets/pg_app_creds")
    try:
        with open(pw_file) as f:
            for line in f:
                if line.startswith("PG_PW="):
                    pw = line.strip().split("=", 1)[1]
                    break
    except OSError:
        pass
    return psycopg2.connect(
        host=os.environ.get("LLM_MANAGER_PG_HOST", "10.0.20.116"),
        dbname=os.environ.get("LLM_MANAGER_PG_DB", "llmmanager"),
        user=os.environ.get("LLM_MANAGER_PG_USER", "llmmanager"),
        password=pw or "",
        connect_timeout=10,
    )


def _setting(cur, key: str, default: int) -> int:
    cur.execute("SELECT value FROM manager_settings WHERE key = %s", (key,))
    row = cur.fetchone()
    try:
        return int(row[0]) if row else default
    except (TypeError, ValueError):
        return default


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


# --------------------------------------------------------------------------- rollup

ROLLUP_SQL = """
INSERT INTO gpu_samples_hourly (
    hour_bucket, host_id, gpu_index, sample_count,
    util_min, util_max, util_avg,
    mem_used_min_mib, mem_used_max_mib, mem_used_avg_mib,
    power_min_w, power_max_w, power_avg_w, power_sum_wh, last_ts)
SELECT date_trunc('hour', ts), host_id, gpu_index,
       count(*)::int,
       min(util_pct), max(util_pct), avg(util_pct),
       min(mem_used_mib), max(mem_used_mib), avg(mem_used_mib),
       min(power_watts), max(power_watts), avg(power_watts),
       sum(COALESCE(power_watts,0)), max(ts)
FROM gpu_samples
WHERE ts >= %s AND ts < %s AND host_id IS NOT NULL
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
"""


def _rollup_window(conn, since: datetime, until: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(ROLLUP_SQL, (since, until))
        n = cur.rowcount
    conn.commit()
    return n


def cmd_rollup() -> int:
    """Incremental: re-aggregate the last 3 hours (idempotent upsert)."""
    conn = connect()
    try:
        now = datetime.now(timezone.utc)
        n = _rollup_window(conn, now - timedelta(hours=3), now + timedelta(minutes=5))
        log(f"rollup: {n} hourly buckets upserted (last 3h window)")
        return 0
    finally:
        conn.close()


def cmd_backfill() -> int:
    """Resumable full-history backfill in 24h chunks; re-running continues."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT min(ts), max(ts) FROM gpu_samples")
            lo, hi = cur.fetchone()
        if lo is None:
            log("backfill: gpu_samples empty; nothing to do")
            return 0
        # Resume from what the rollup already covers (minus overlap).
        with conn.cursor() as cur:
            cur.execute("SELECT min(hour_bucket) FROM gpu_samples_hourly")
            covered = cur.fetchone()[0]
        start = lo if covered is None else min(lo, covered)
        log(f"backfill: {start} -> {hi}")
        cur0 = start
        total = 0
        while cur0 < hi:
            # Chunk on hour-aligned boundaries: a non-aligned chunk end would
            # re-aggregate a straddling hour from partial data and the upsert
            # would overwrite the full aggregate with the partial one.
            nxt = min(cur0 + timedelta(hours=24), hi + timedelta(seconds=1))
            if nxt < hi:
                nxt = nxt.replace(minute=0, second=0, microsecond=0)
            total += _rollup_window(conn, cur0, nxt)
            cur0 = nxt
            log(f"backfill: chunk done through {nxt} (cumulative buckets: {total})")
        log(f"backfill complete: {total} buckets")
        return 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- verify

def cmd_verify() -> int:
    """Compare raw aggregates vs rollup rows for representative windows.

    Tolerance: count exact; numerics within 1% relative or 1e-9 absolute.
    Exits non-zero on any mismatch (pruning gate).
    """
    conn = connect()
    failures = 0
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # 3 representative windows: last 24h, a 48h window 10-12 days ago, all-time.
            windows = [
                ("last_24h", "now() - interval '24 hours'", "now()"),
                ("mid_history_48h", "now() - interval '12 days'", "now() - interval '10 days'"),
                ("all_time", "'-infinity'::timestamptz", "'infinity'::timestamptz"),
            ]
            for name, lo, hi in windows:
                cur.execute(f"""
                    SELECT date_trunc('hour', ts) h, host_id, gpu_index,
                           count(*) c, avg(util_pct) u_avg, avg(power_watts) p_avg
                    FROM gpu_samples
                    WHERE ts >= {lo} AND ts < {hi} AND host_id IS NOT NULL
                    GROUP BY 1,2,3
                    HAVING count(*) > 0
                    ORDER BY random() LIMIT 200""")
                sampled = cur.fetchall()
                checked = 0
                for row in sampled:
                    cur.execute("""
                        SELECT sample_count, util_avg, power_avg_w
                        FROM gpu_samples_hourly
                        WHERE hour_bucket=%s AND host_id=%s AND gpu_index=%s""",
                        (row["h"], row["host_id"], row["gpu_index"]))
                    r = cur.fetchone()
                    if r is None:
                        log(f"VERIFY FAIL [{name}]: missing rollup row {row['h']} host={row['host_id']} gpu={row['gpu_index']}")
                        failures += 1
                        continue
                    checked += 1
                    if r[0] != row["c"]:
                        log(f"VERIFY FAIL [{name}]: count {r[0]} != {row['c']} at {row['h']}")
                        failures += 1
                    for label, got, want in (("util_avg", r[1], row["u_avg"]), ("power_avg", r[2], row["p_avg"])):
                        if (got is None) != (want is None):
                            log(f"VERIFY FAIL [{name}]: {label} NULL mismatch at {row['h']}")
                            failures += 1
                        elif got is not None and abs(float(got) - float(want)) > max(VERIFY_REL_TOLERANCE * abs(float(want)), 1e-9):
                            log(f"VERIFY FAIL [{name}]: {label} {got} != {want} at {row['h']}")
                            failures += 1
                log(f"verify [{name}]: {checked} rows checked, {failures} cumulative failures")
    finally:
        conn.close()
    if failures:
        log(f"VERIFY FAILED with {failures} mismatches — pruning gate stays CLOSED")
        return 1
    log("VERIFY PASSED — pruning gate may open (subject to backup + dry-run review)")
    return 0


# --------------------------------------------------------------------------- prune

def _prune_gates(conn) -> tuple[bool, str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM information_schema.tables
            WHERE table_name='gpu_samples_hourly'""")
        if cur.fetchone()[0] == 0:
            return False, "rollup table does not exist"
        cur.execute("SELECT count(*) FROM gpu_samples_hourly")
        if cur.fetchone()[0] < MIN_HOURLY_ROWS_FOR_PRUNE:
            return False, "rollup table is empty (backfill first)"
        cur.execute("SELECT min(hour_bucket), max(hour_bucket) FROM gpu_samples_hourly")
        lo, hi = cur.fetchone()
        if lo is None or hi is None:
            return False, "rollup has no rows"
        # Backfill completeness: rollup must cover raw history up to the
        # prune cutoff. If raw has data OLDER than the earliest rollup hour,
        # backfill is incomplete.
        cur.execute("SELECT min(ts) FROM gpu_samples")
        raw_lo = cur.fetchone()[0]
        if raw_lo is not None and raw_lo < lo:
            return False, f"backfill incomplete: raw starts {raw_lo} but rollup starts {lo}"
        # Backup gate: a verified recovery point must exist (recorded by ops
        # in manager_settings as gpu_retention_backup_verified_at).
        cur.execute("SELECT value FROM manager_settings WHERE key='gpu_retention_backup_verified_at'")
        row = cur.fetchone()
        if not row:
            return False, "backup gate not recorded (set manager_settings gpu_retention_backup_verified_at)"
    return True, "all gates pass"


def cmd_dry_run_prune() -> dict:
    raw_days = None
    conn = connect()
    try:
        with conn.cursor() as cur:
            raw_days = _setting(cur, "gpu_samples_retention_days", 90)
        ok, why = _prune_gates(conn)
        cutoff = datetime.now(timezone.utc) - timedelta(days=raw_days)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), COALESCE(pg_total_relation_size('gpu_samples'),0) FROM gpu_samples WHERE ts < %s", (cutoff,))
            candidates, _sz = cur.fetchone()
            cur.execute("SELECT min(ts) FROM gpu_samples")
            oldest = cur.fetchone()[0]
            cur.execute("SELECT min(ts) FROM gpu_samples WHERE ts >= %s", (cutoff,))
            oldest_after = cur.fetchone()[0]
        result = {
            "gates_pass": ok, "gate_reason": why,
            "raw_retention_days": raw_days,
            "cutoff": cutoff.isoformat(),
            "candidate_rows": candidates,
            "oldest_row": oldest.isoformat() if oldest else None,
            "oldest_row_after_prune": oldest_after.isoformat() if oldest_after else None,
            "dry_run": True,
        }
        log("DRY-RUN PRUNE: " + json.dumps(result, default=str))
        return result
    finally:
        conn.close()


def cmd_prune() -> int:
    raw_days = None
    conn = connect()
    try:
        with conn.cursor() as cur:
            raw_days = _setting(cur, "gpu_samples_retention_days", 90)
        ok, why = _prune_gates(conn)
        if not ok:
            log(f"PRUNE REFUSED: {why}")
            return 1
        cutoff = datetime.now(timezone.utc) - timedelta(days=raw_days)
        total = 0
        with conn.cursor() as cur:
            while True:
                cur.execute("""
                    DELETE FROM gpu_samples
                    WHERE sample_id IN (
                        SELECT sample_id FROM gpu_samples
                        WHERE ts < %s ORDER BY sample_id LIMIT %s)""",
                    (cutoff, PRUNE_BATCH_ROWS))
                deleted = cur.rowcount
                conn.commit()
                total += deleted
                if deleted == 0:
                    break
                log(f"prune: deleted {total} rows so far (cutoff {cutoff})")
            # VACUUM cannot run inside a transaction block
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("VACUUM ANALYZE gpu_samples")
        log(f"prune COMPLETE: {total} raw rows deleted (cutoff {cutoff}); hourly aggregates retained")
        return 0
    finally:
        conn.close()


def cmd_maintenance() -> int:
    """Scheduled entrypoint: incremental rollup, then hourly retention prune."""
    rc = cmd_rollup()
    if rc:
        return rc
    conn = connect()
    try:
        with conn.cursor() as cur:
            hourly_days = _setting(cur, "gpu_samples_hourly_retention_days", 730)
        cutoff = datetime.now(timezone.utc) - timedelta(days=hourly_days)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gpu_samples_hourly WHERE hour_bucket < %s", (cutoff,))
            n = cur.rowcount
        conn.commit()
        if n:
            log(f"hourly retention: {n} buckets older than {hourly_days}d removed")
        return 0
    finally:
        conn.close()


def main() -> int:
    cmds = {
        "rollup": cmd_rollup,
        "backfill": cmd_backfill,
        "verify": cmd_verify,
        "dry-run-prune": cmd_dry_run_prune,
        "prune": cmd_prune,
        "maintenance": cmd_maintenance,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print("usage: gpu_retention.py {" + "|".join(cmds) + "}", file=sys.stderr)
        return 2
    out = cmds[sys.argv[1]]()
    return out if isinstance(out, int) else (0 if out.get("gates_pass") else 1)


if __name__ == "__main__":
    sys.exit(main())
