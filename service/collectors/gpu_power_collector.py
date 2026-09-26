#!/usr/bin/env python3
"""LLM Manager GPU power collector (Phase 5 minimal).

Runs on VM114 as a systemd service. Every GPU_SAMPLE_SECONDS:
  - For each configured vLLM host, SSH via control key -> llm-control.sh nvidia
  - Parse per-GPU: index, util%, mem used MiB, power W
  - Batch-insert into llmmanager.gpu_samples
  - Roll 1-minute averages into host_power_rollup_1m
Failure of any host is logged, never fatal. DSN/creds read from secret files.
"""
import os
import subprocess
import time
import psycopg2
from datetime import datetime, timezone

SECRETS = "/etc/llm-manager/secrets"
PG_HOST = "10.0.20.116"
PG_DB = "llmmanager"
PG_USER = "llmmanager"
CONTROL_KEY = f"{SECRETS}/keys/id_ed25519"
SAMPLE_SECONDS = int(os.environ.get("GPU_SAMPLE_SECONDS", "20"))
HOSTS = [
    ("10.0.20.161", "MIAM-00111/VM103"),
    ("10.0.20.162", "MIAM-00112/VM401"),
    ("10.0.20.163", "MIAM-00143/VM109"),
    ("10.0.20.164", "MIAM-00144/VM111"),
]

HOST_IDS = {}


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


def sec(name):
    try:
        with open(os.path.join(SECRETS, name)) as f:
            return f.read().strip()
    except Exception:
        return None


def pg_connect():
    pw = None
    try:
        with open(f"{SECRETS}/pg_app_creds") as f:
            for line in f:
                if line.startswith("PG_PW="):
                    pw = line.strip().split("=", 1)[1]
    except Exception:
        pass
    return psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER,
                            password=pw, connect_timeout=5)


def control_nvidia(ip):
    """Run llm-control.sh nvidia on a guest via the control key. Returns stdout."""
    cmd = ["ssh", "-i", CONTROL_KEY,
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=6", "-o", "BatchMode=yes",
           f"root@{ip}", "/opt/llm-control/llm-control.sh nvidia"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:120])
    return p.stdout


def parse_nvidia(out):
    """Parse control-agent CSV lines 'idx, util%, memMiB, W'."""
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            idx = int(parts[0])
            util = float(parts[1].rstrip("%"))
            mem = float(parts[2].split()[0])
            watts = float(parts[3].split()[0])
            rows.append((idx, util, mem, watts))
        except (ValueError, IndexError):
            continue
    return rows


def ensure_host_ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT host_id, guest_ip FROM hosts")
        for hid, ip in cur.fetchall():
            HOST_IDS[ip] = hid


def insert_samples(conn, rows):
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO gpu_samples (ts, host_id, gpu_index, util_pct, mem_used_mib, power_watts) "
            "VALUES (now(), %s, %s, %s, %s, %s)", rows)
    conn.commit()
    return len(rows)


def rollup_minute(conn):
    """Average the last 60s of samples into the 1-minute rollup table (idempotent per minute)."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO host_power_rollup_1m (ts, host_id, gpu_watts_avg, gpu_watts_peak)
            SELECT date_trunc('minute', now()) - interval '1 minute', host_id,
                   avg(power_watts), max(power_watts)
            FROM gpu_samples
            WHERE ts >= date_trunc('minute', now()) - interval '1 minute'
              AND ts <  date_trunc('minute', now())
            GROUP BY host_id
            ON CONFLICT DO NOTHING""")
    conn.commit()


def main():
    conn = pg_connect()
    ensure_host_ids(conn)
    log(f"collector start: {len(HOSTS)} hosts, interval {SAMPLE_SECONDS}s")
    minute_counter = 0
    while True:
        cycle_rows = []
        for ip, name in HOSTS:
            try:
                parsed = parse_nvidia(control_nvidia(ip))
                hid = HOST_IDS.get(ip)
                for idx, util, mem, watts in parsed:
                    cycle_rows.append((hid, idx, util, mem, watts))
                log(f"{name} ok: {len(parsed)} GPUs, "
                    f"sum={sum(r[3] for r in parsed):.1f}W")
            except Exception as e:
                log(f"{name} ({ip}) sample failed: {e}")
        try:
            n = insert_samples(conn, cycle_rows)
            if n:
                log(f"inserted {n} samples")
            minute_counter += 1
            if minute_counter >= max(1, 60 // SAMPLE_SECONDS):
                rollup_minute(conn)
                minute_counter = 0
                log("1-minute rollup written")
        except Exception as e:
            log(f"DB write failed: {e}")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(5)
            conn = pg_connect()
            ensure_host_ids(conn)
        time.sleep(SAMPLE_SECONDS)


if __name__ == "__main__":
    main()
