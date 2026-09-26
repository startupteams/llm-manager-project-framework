#!/usr/bin/env python3
"""Emporia facility-power adapter (Phase 6) — LLM Manager.

Read-only cloud collector via PyEmVue. NEVER touches the local PDUs
(.151-.153) — they stay free for control traffic (Jordan's rule).
Login + per-minute channel usage every 300s (5 min = 12 samples/hour).

Writes to PostgreSQL (llmmanager @ 10.0.20.116):
  emporia_devices           device/channel inventory snapshot
  emporia_channel_map       channel -> compute|cooling|other (seeded, user-correctable)
  facility_power_samples    per-channel kWh-interval rows (usage is Wh/min)
  facility_power_rollup_1d  daily per-channel kWh + avg W (upsert by day+channel)

Usage semantics (verified live 2026-09-10):
  get_device_list_usage(gids, scale='1MIN', unit='KilowattHours', instant=None)
  -> {device_gid: VueUsageDevice}; dev.channels is a DICT keyed by channel_num
     string ('1','2',...,'1,2,3','Balance'); ch.usage is kWh consumed during
     the minute (e.g. 0.0111 kWh/min = ~667 W average).

DB is SQL_ASCII: all string literals sanitized to ASCII.
"""
import json, os, sys, time, re
from datetime import datetime, timezone, date

CREDS = "/etc/llm-manager/secrets/emporia_creds"
LOG = "/var/log/llm-manager/emporia.log"
PG = dict(host="10.0.20.116", port=5432, dbname="llmmanager", user="llmmanager")

_NONASCII = re.compile(r"[^\x20-\x7e]")

def _asc(s):
    if s is None:
        return None
    return _NONASCII.sub("?", str(s))[:120]

def _log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        print(line, flush=True)

def _pg():
    pgpw = ""
    for line in open(CREDS):
        k, _, v = line.strip().partition("=")
        if k == "PG_PW":
            pgpw = v
    # PG password lives in pg_app_creds (same dir), not the emporia file
    if not pgpw:
        for line in open(os.path.join(os.path.dirname(CREDS), "pg_app_creds")):
            k, _, v = line.strip().partition("=")
            if k == "PG_PW":
                pgpw = v
    import psycopg2
    return psycopg2.connect(host=PG["host"], dbname=PG["dbname"], user=PG["user"],
                            password=pgpw, connect_timeout=5)

def _login():
    email = pw = ""
    for line in open(CREDS):
        k, _, v = line.strip().partition("=")
        if k == "EMPORIA_EMAIL":
            email = v
        elif k == "EMPORIA_PASSWORD":
            pw = v
    from pyemvue.pyemvue import PyEmVue
    vue = PyEmVue()
    vue.customer = None
    vue.login(username=email, password=pw)
    return vue

# --- channel role map (from 2026-09-10 inventory; user-correctable) ---------
# device "Marion 3x240v 30A" (gid 631093), WAT001 monitor unit
CHANNEL_ROLES = {
    ("631093", "1"): ("compute", "Server#1 rack PDU circuit"),
    ("631093", "2"): ("compute", "Server#2 rack PDU circuit"),
    ("631093", "3"): ("compute", "Server#3 rack PDU circuit"),
    ("631093", "4"): ("cooling", "36k 3 Ton mini split"),
    ("631093", "1,2,3"): ("other", "3-phase service mains aggregate"),
    ("631093", "Balance"): ("other", "unmonitored balance of service"),
}

def ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS emporia_devices (
        device_gid BIGINT, channel_num TEXT, channel_name TEXT, model TEXT,
        last_seen TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (device_gid, channel_num))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS emporia_channel_map (
        device_gid BIGINT, channel_num TEXT, mapped_role TEXT, mapped_target TEXT,
        confidence TEXT DEFAULT 'high', notes TEXT,
        PRIMARY KEY (device_gid, channel_num))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS facility_power_samples (
        ts TIMESTAMPTZ, device_gid BIGINT, channel_num TEXT, watts NUMERIC,
        kwh_interval NUMERIC, PRIMARY KEY (ts, device_gid, channel_num))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS facility_power_rollup_1d (
        day DATE, device_gid BIGINT, channel_num TEXT, kwh NUMERIC, avg_watts NUMERIC,
        PRIMARY KEY (day, device_gid, channel_num))""")
    for (gid, num), (role, target) in CHANNEL_ROLES.items():
        cur.execute("""INSERT INTO emporia_channel_map (device_gid, channel_num, mapped_role, mapped_target, confidence, notes)
                       VALUES (%s,%s,%s,%s,'high','seeded from 2026-09-10 inventory')
                       ON CONFLICT (device_gid, channel_num) DO NOTHING""", (gid, num, role, target))

def collect_cycle(vue):
    devices = vue.get_devices()
    gids = [int(d.device_gid) for d in devices]
    usage = vue.get_device_list_usage(gids, scale='1MIN', unit='KilowattHours', instant=None)
    now = datetime.now(timezone.utc)
    rows, inventory = [], []
    for gid, dev in usage.items():
        for cnum, ch in (dev.channels or {}).items():
            name = getattr(ch, "name", None)
            u = getattr(ch, "usage", None)
            if u is None:
                continue
            rows.append({
                "device_gid": int(gid), "channel_num": _asc(cnum),
                "channel_name": _asc(name), "kwh_interval": float(u),
            })
            inventory.append((int(gid), _asc(cnum), _asc(name), "WAT001"))
    return rows, inventory

def write_cycle(rows, inventory):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            for gid, num, name, model in inventory:
                cur.execute("""INSERT INTO emporia_devices (device_gid, channel_num, channel_name, model, last_seen)
                               VALUES (%s,%s,%s,%s, now())
                               ON CONFLICT (device_gid, channel_num) DO UPDATE SET channel_name=EXCLUDED.channel_name, last_seen=now()""",
                            (gid, num, name, model))
            ts = datetime.now(timezone.utc)
            for r in rows:
                cur.execute("""INSERT INTO facility_power_samples (ts, device_gid, channel_num, watts, kwh_interval)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (ts, r["device_gid"], r["channel_num"], round(r["kwh_interval"] * 60000.0, 1), r["kwh_interval"]))
            # daily rollup upsert (aggregate from the samples table)
            cur.execute("""INSERT INTO facility_power_rollup_1d (day, device_gid, channel_num, kwh, avg_watts)
                           SELECT CURRENT_DATE, s.device_gid, s.channel_num,
                                  SUM(s.kwh_interval), AVG(s.watts)
                           FROM facility_power_samples s
                           WHERE s.ts::date = CURRENT_DATE
                           GROUP BY s.device_gid, s.channel_num
                           ON CONFLICT (day, device_gid, channel_num) DO UPDATE
                           SET kwh=EXCLUDED.kwh, avg_watts=EXCLUDED.avg_watts""")
        conn.commit()
    finally:
        conn.close()

def main():
    loop = "--loop" in sys.argv
    failures = 0
    while True:
        try:
            vue = _login()
            rows, inventory = collect_cycle(vue)
            write_cycle(rows, inventory)
            failures = 0
            _log(f"cycle ok: {len(rows)} channels written")
            if not loop:
                print(f"cycle ok: {len(rows)} channels", flush=True)
                break
            time.sleep(300 if failures == 0 else 300)
        except Exception as e:
            failures += 1
            _log(f"cycle FAILED ({failures} consecutive): {e.__class__.__name__}: {str(e)[:200]}")
            if not loop:
                print(f"FAILED: {e.__class__.__name__}: {e}", file=sys.stderr)
                sys.exit(1)
            time.sleep(300 if failures < 4 else 900)

if __name__ == "__main__":
    main()