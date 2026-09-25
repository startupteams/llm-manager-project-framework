#!/usr/bin/env python3
"""LLM Manager v0.11 recovery engine + registry sync loop.

Runs as llm-manager-recovery.service. Every tick:
  1. sync_registry(): probe all hosts, update model_registry health/routable
  2. enforce desired-state policy (plan §B2):
       - MANAGED + RUNNING + unexpectedly stopped  -> bounded VM start recovery
       - MANAGED + STOPPED_INTENTIONAL             -> leave off, do not route
  3. bounded service recovery for running VMs whose model service is down
All actions audited into recovery_events. Never loops forever (§B4).
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

sys.path.insert(0, "/opt/llm-manager/app")
from v011_core import (NODE_MAP, pg, vm_status, vm_power, probe_host,  # noqa: E402
                       sync_registry, HOST_IPS)

VM_START_MAX = 2
SERVICE_RESTART_MAX = 2
SERVICE_RESTART_COOLDOWN_S = 15 * 60
TICK_SECONDS = 60

_audit_file = "/var/log/llm-manager/recovery-engine.log"


def audit(ip, event_type, detail):
    line = {"ts": datetime.now(timezone.utc).isoformat(), "ip": ip,
            "event_type": event_type, "detail": detail}
    try:
        os.makedirs(os.path.dirname(_audit_file), exist_ok=True)
        with open(_audit_file, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass
    conn = pg()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO recovery_events (ip, event_type, detail)
                               VALUES (%s,%s,%s)""", (ip, event_type, json.dumps(detail)))
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()


def recent_count(ip, event_type, minutes=30):
    conn = pg()
    if not conn:
        return 99  # fail-safe: no DB -> no recovery actions
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) FROM recovery_events
                           WHERE ip=%s AND event_type=%s
                             AND created_at > now() - (%s || ' minutes')::interval""",
                        (ip, event_type, str(minutes)))
            return cur.fetchone()[0]
    finally:
        conn.close()


def recover_vm_start(ip, desired):
    """Bounded VM start when unexpectedly stopped (§B2)."""
    if recent_count(ip, "recovery_vm_start") >= VM_START_MAX:
        audit(ip, "recovery_failed", {"reason": "vm start attempts exhausted",
                                      "desired": desired})
        return
    audit(ip, "recovery_vm_start", {"reason": "unexpected stop", "attempt": recent_count(ip, "recovery_vm_start") + 1})
    vm_power(ip, "start")
    # wait for boot + service
    for _ in range(40):
        time.sleep(15)
        st = vm_status(ip).get("status")
        if st == "running":
            p = probe_host(ip, timeout=6)
            if p.get("health_completion") == "ok":
                audit(ip, "recovery_success", {"method": "vm_start"})
                return


def recover_service(ip):
    """Bounded systemd service restart via control agent."""
    if recent_count(ip, "recovery_service_restart") >= SERVICE_RESTART_MAX:
        audit(ip, "recovery_failed", {"reason": "service restart attempts exhausted"})
        return
    last = recent_count(ip, "recovery_service_restart", minutes=3)
    if last and SERVICE_RESTART_COOLDOWN_S > 180:
        # respect cooldown between restart attempts
        if recent_count(ip, "recovery_service_restart", minutes=3) >= 1:
            return
    audit(ip, "recovery_service_restart", {"attempt": recent_count(ip, "recovery_service_restart") + 1})
    _control_restart(ip)
    for _ in range(60):
        time.sleep(10)
        p = probe_host(ip, timeout=6)
        if p.get("health_completion") == "ok":
            audit(ip, "recovery_success", {"method": "service_restart"})
            return


def _control_restart(ip):
    import subprocess
    key = os.path.join("/etc/llm-manager/secrets/keys/id_ed25519")
    cmd = ["ssh", "-i", key,
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/root/.ssh/known_hosts_llm",
           "-o", "ConnectTimeout=6", "-o", "BatchMode=yes",
           f"root@{ip}", "/opt/llm-control/llm-control.sh restart"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        pass


def tick():
    s = sync_registry()
    if s.get("error"):
        print(f"[recovery] registry sync error: {s['error']}", flush=True)
        return
    conn = pg()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT h.host_id, h.guest_ip, h.name, h.desired_power_state, h.management_mode,
                          COALESCE(h.desired_service_state, 'SERVING') AS desired_service_state
                          FROM hosts h
                          ORDER BY h.guest_ip""")
            hosts = cur.fetchall()
    finally:
        conn.close()
    states = {h["ip"]: h for h in s.get("hosts", [])}
    for hid, ip, name, desired, mode, svc_state in hosts:
        vm = vm_status(ip)
        vmst = vm.get("status")
        p = states.get(ip, {})
        if mode != "MANAGED":
            continue
        if desired == "STOPPED_INTENTIONAL":
            # §K3: leave it off; do not count as outage. If VM still running, log info once.
            if vmst == "running":
                audit(ip, "intentional_state_pending", {"vm_status": vmst, "note": "awaiting operator power-off"})
            continue
        # desired RUNNING:
        if vmst != "running":
            audit(ip, "outage_detected", {"vm_status": vmst, "desired": desired})
            recover_vm_start(ip, desired)
        elif p.get("state") == "unhealthy":
            if svc_state == "MAINTENANCE":
                audit(ip, "maintenance_skip", {"probe_state": p.get("state"), "note": "recovery suppressed in MAINTENANCE"})
                continue
            audit(ip, "service_degraded", {"probe_state": p.get("state")})
            recover_service(ip)


def main():
    print(f"[recovery] v0.11 recovery engine started pid={os.getpid()}", flush=True)
    # initial registry stamp
    try:
        sync_registry()
    except Exception:
        traceback.print_exc()
    while True:
        t0 = time.time()
        try:
            tick()
        except Exception:
            traceback.print_exc()
        dt = time.time() - t0
        time.sleep(max(10, TICK_SECONDS - dt))


if __name__ == "__main__":
    main()
