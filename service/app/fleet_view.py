"""Model Fleet visibility — window-5 plan §3.

/api/fleet: ALL model-serving/model-capable VMs from authoritative sources —
the hosts table (inventory: node/vmid/desired states), live probe_vllm health,
and model_registry (registered models, routes, staleness). NO hard-coded list;
inventory rows drive everything.

Operator states (§3.2) — never collapse "VM powered on" into "model healthy":

  SERVING           live probe healthy + desired SERVING + desired RUNNING
  ONLINE_IDLE       VM reachable via probe error? No: probe healthy but model
                    route demoted / desired_service_state != SERVING
  STOPPED_EXPECTED  probe unreachable + desired_power STOPPED(_INTENTIONAL)
  UNREACHABLE       probe unreachable + desired_power RUNNING
  MISCONFIGURED     probe returned an HTTP error status
  STALE             routable model last_verified > 48h old
"""
from __future__ import annotations

import concurrent.futures as cf
import time
from datetime import datetime, timezone
from typing import Any

import psycopg2

from main import PG_DB, PG_HOST, PG_USER, VLLM_HOSTS, _active_hosts, _load_pg_pw, probe_vllm

_STALE_HOURS = 48

# Operator-state names (rendered verbatim by the Fleet page)
SERVING = "SERVING"
ONLINE_IDLE = "ONLINE_IDLE"
STARTING = "STARTING"
STOPPED_EXPECTED = "STOPPED_EXPECTED"
UNREACHABLE = "UNREACHABLE"
MISCONFIGURED = "MISCONFIGURED"
STALE = "STALE"


def _inventory_rows() -> dict[str, dict[str, Any]]:
    """hosts table = the authoritative inventory (node/vmid/desired states)."""
    pw = _load_pg_pw()
    if not pw:
        return {}
    try:
        c = psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER, password=pw,
                             connect_timeout=4)
    except Exception:
        return {}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT host_id, name, guest_ip, gpu_count, gpu_model, node, vmid,
                       desired_power_state, desired_service_state, management_mode
                FROM hosts ORDER BY host_id""")
            rows = {}
            for (hid, name, ip, gpus, gmodel, node, vmid, dps, dss, mm) in cur.fetchall():
                rows[ip] = {
                    "host_id": hid, "inventory_name": name, "gpu_count": gpus,
                    "gpu_model": gmodel, "node": node, "vmid": vmid,
                    "desired_power_state": dps, "desired_service_state": dss,
                    "management_mode": mm,
                }
            return rows
    finally:
        c.close()


def _models_by_host() -> dict[str, list[dict[str, Any]]]:
    """model_registry rows grouped by host ip (backend_url host)."""
    pw = _load_pg_pw()
    if not pw:
        return {}
    try:
        c = psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER, password=pw,
                             connect_timeout=4)
    except Exception:
        return {}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT logical_model_name, host_id, engine, health, routable,
                       is_alias, alias_target, context_limit, backend_url, last_verified
                FROM model_registry ORDER BY logical_model_name""")
            out: dict[str, list[dict[str, Any]]] = {}
            for (name, hid, engine, health, routable, alias, target, ctx, url, verified) in cur.fetchall():
                if not url or not url.startswith("http"):
                    continue
                host_ip = url.split("//")[1].split(":")[0].split("/")[0]
                out.setdefault(host_ip, []).append({
                    "model": name, "host_id": hid, "engine": engine,
                    "health": health, "routable": bool(routable),
                    "is_alias": bool(alias), "alias_target": target,
                    "context_limit": ctx,
                    "last_verified": verified.isoformat() if verified else None,
                })
            return out
    finally:
        c.close()


def _derive_state(probe_status: str, desired_power: str, desired_service: str,
                  models: list[dict[str, Any]]) -> str:
    """§3.2 operator state — powered-on and model-healthy are DIFFERENT facts."""
    dpu = (desired_power or "").upper()
    dsu = (desired_service or "").upper()
    reachable = probe_status == "healthy"
    if probe_status.startswith("http_"):
        return MISCONFIGURED
    if not reachable:
        if dpu in ("STOPPED", "STOPPED_INTENTIONAL"):
            return STOPPED_EXPECTED
        return UNREACHABLE
    if dpu in ("STOPPED", "STOPPED_INTENTIONAL") or dsu != "SERVING":
        return ONLINE_IDLE
    routable = [m for m in models if m.get("routable") and not m.get("is_alias")]
    if not routable:
        return ONLINE_IDLE
    for m in routable:
        lv = m.get("last_verified")
        if lv:
            try:
                age_h = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(lv.replace("Z", "+00:00"))).total_seconds() / 3600
                if age_h > _STALE_HOURS:
                    return STALE
            except ValueError:
                pass
    return SERVING


def fleet_view() -> dict[str, Any]:
    """Build the full fleet view — the Model Fleet page's data contract."""
    inventory = _inventory_rows()
    models_by_host = _models_by_host()
    # probe the full inventory (hosts table) UNION the legacy VLLM_HOSTS list —
    # a host registered in either source stays visible (§3.4)
    ips = {h["ip"] for h in VLLM_HOSTS} | set(inventory)
    named = {h["ip"]: h for h in VLLM_HOSTS}
    hosts: list[dict[str, Any]] = []
    probe_by_ip = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        probes = {}
        for ip in ips:
            h = named.get(ip) or {"name": inventory.get(ip, {}).get("inventory_name", ip), "ip": ip}
            probes[ex.submit(probe_vllm, h)] = ip
        for f, ip in probes.items():
            probe_by_ip[ip] = f.result()

    now_iso = datetime.now(timezone.utc).isoformat()
    for ip in sorted(ips):
        inv = inventory.get(ip, {})
        probe = probe_by_ip.get(ip, {})
        models = models_by_host.get(ip, [])
        state = _derive_state(probe.get("status", "error"),
                              inv.get("desired_power_state", "RUNNING"),
                              inv.get("desired_service_state", "SERVING"),
                              models)
        last_seen = probe.get("latency_ms") if probe.get("status") == "healthy" else None
        hosts.append({
            "ip": ip,
            "name": inv.get("inventory_name") or probe.get("name") or ip,
            "node": inv.get("node"), "vmid": inv.get("vmid"),
            "state": state,
            "probe_status": probe.get("status"),
            "probe_error": probe.get("error"),
            "latency_ms": probe.get("latency_ms"),
            "probe_model": probe.get("model"),
            "probe_max_model_len": probe.get("max_model_len"),
            "desired_power_state": inv.get("desired_power_state", "RUNNING"),
            "desired_service_state": inv.get("desired_service_state", "SERVING"),
            "management_mode": inv.get("management_mode"),
            "gpu_count": inv.get("gpu_count"), "gpu_model": inv.get("gpu_model"),
            "host_id": inv.get("host_id"),
            "models": models,
            "last_seen": now_iso if last_seen is not None else None,
        })
    return {
        "hosts": hosts,
        "states": {s: sum(1 for h in hosts if h["state"] == s)
                   for s in (SERVING, ONLINE_IDLE, STOPPED_EXPECTED, UNREACHABLE,
                             MISCONFIGURED, STALE)},
        "inventory_source": "db:hosts" if inventory else "legacy:vllm_hosts",
        "ts": time.time(),
    }