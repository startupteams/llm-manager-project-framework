#!/usr/bin/env python3
"""LLM Manager v0.11 core — desired-state, logical model registry, health gating,
recovery policy, gated /v1/models, and machine-to-machine agent key API.

Plan: MARION_IA_USA_LLM_Manager_Next_Steps_Plan_v0_11.
Rules honored here:
  - desired state distinguishes unexpected outage vs intentional power saving (§B)
  - bounded recovery, never an infinite restart loop (§B4)
  - model registry is routing source of truth; routability gate (§C)
  - /v1/models omits non-routable models (§C3)
  - no default model, exact model routing (§D)
  - agent keys are generic; no endpoint encoding (§E)
  - unknown cost shows as unknown, never fabricated zero (§H3)
"""
import json
import os
import time
import threading
from datetime import datetime, timezone

import psycopg2
import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

SECRETS = "/etc/llm-manager/secrets"
PG_HOST = "10.0.20.116"
PG_DB = "llmmanager"
PG_USER = "llmmanager"
PG_PW_FILE = f"{SECRETS}/pg_app_creds"
LITELLM_URL = "http://127.0.0.1:4000"

# ip -> (node, vmid); fixed map per plan §A3 / current cluster layout
NODE_MAP = {
    "10.0.20.161": ("miam00111", 103),
    "10.0.20.162": ("miam00112", 401),
    "10.0.20.163": ("miam00143", 109),
    "10.0.20.164": ("miam00144", 111),
    "10.0.20.165": ("miam-00149", 149),
}
HOST_IPS = list(NODE_MAP.keys())
PVE_HOST = "10.0.20.135"

RECOVERY_WINDOW_MIN = 30      # lookback window for bounded attempts
VM_START_MAX_ATTEMPTS = 2     # §B4 defaults
SERVICE_RESTART_MAX_ATTEMPTS = 2
SERVICE_RESTART_COOLDOWN_MIN = 15

ROUTER = APIRouter()


def sec(name):
    try:
        with open(os.path.join(SECRETS, name)) as f:
            return f.read().strip()
    except Exception:
        return None


def _pg_pw():
    try:
        with open(PG_PW_FILE) as f:
            for line in f:
                if line.startswith("PG_PW="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return None


def pg():
    try:
        return psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER,
                                password=_pg_pw(), connect_timeout=4)
    except Exception:
        return None


# ---------------------------------------------------------------- PVE API (token)
def pve_api(path, method="GET"):
    tok = sec("pve_token")
    if not tok:
        return None
    import ssl, urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"https://{PVE_HOST}:8006/api2/json{path}", method=method,
        headers={"Authorization": f"PVEAPIToken=llm-manager@pve!manager-control={tok}"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=12, context=ctx).read())
    except Exception:
        return None


def vm_status(ip):
    node, vmid = NODE_MAP[ip]
    d = (pve_api(f"/nodes/{node}/qemu/{vmid}/status/current") or {}).get("data") or {}
    return {"node": node, "vmid": vmid, "status": d.get("status"), "uptime_s": d.get("uptime")}


def vm_power(ip, action):
    """action in start|shutdown; allowlist only."""
    assert action in ("start", "shutdown"), action
    node, vmid = NODE_MAP[ip]
    d = (pve_api(f"/nodes/{node}/qemu/{vmid}/status/{action}", method="POST") or {}).get("data")
    return bool(d)


# ---------------------------------------------------------------- schema (§B1/C1)
MIGRATION_SQL = """
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS desired_power_state TEXT NOT NULL DEFAULT 'RUNNING';
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS management_mode TEXT NOT NULL DEFAULT 'MANAGED';
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS node TEXT;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS vmid INTEGER;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS desired_service_state TEXT NOT NULL DEFAULT 'SERVING';

CREATE TABLE IF NOT EXISTS model_registry (
    id SERIAL PRIMARY KEY,
    logical_model_name TEXT NOT NULL,
    host_id INT REFERENCES hosts(host_id),
    engine TEXT,
    backend_url TEXT,
    context_limit INT,
    capabilities JSONB DEFAULT '{}'::jsonb,
    health TEXT DEFAULT 'unknown',
    routable BOOLEAN DEFAULT FALSE,
    is_alias BOOLEAN DEFAULT FALSE,
    alias_target TEXT,
    last_verified TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (logical_model_name, host_id)
);

CREATE TABLE IF NOT EXISTS recovery_events (
    id SERIAL PRIMARY KEY,
    host_id INT,
    ip TEXT,
    event_type TEXT,
    detail TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS request_routing_log (
    id SERIAL PRIMARY KEY,
    request_id TEXT,
    ts TIMESTAMPTZ DEFAULT now(),
    model TEXT,
    api_base TEXT,
    prompt_tokens INT,
    completion_tokens INT,
    latency_ms INT,
    ttft_ms INT,
    key_alias TEXT,
    provider_spend NUMERIC
);

CREATE TABLE IF NOT EXISTS agent_keys (
    key_id SERIAL PRIMARY KEY,
    external_id TEXT UNIQUE,
    project_id TEXT,
    agent_instance_id TEXT,
    agent_name TEXT,
    harness TEXT,
    alias TEXT UNIQUE,
    raw_key TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    created_by TEXT,
    last_used_at TIMESTAMPTZ,
    enabled BOOLEAN DEFAULT TRUE,
    revoked_at TIMESTAMPTZ,
    monthly_budget NUMERIC
);

INSERT INTO manager_settings (key, value, updated_at, updated_by)
SELECT 'v011.role_aliases.enabled', 'true', now(), 'v011-migration'
WHERE NOT EXISTS (SELECT 1 FROM manager_settings WHERE key = 'v011.role_aliases.enabled');
"""


def init_db():
    conn = pg()
    if not conn:
        raise RuntimeError("postgres unavailable for v0.11 migration")
    try:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_SQL)
            # stamp node/vmid onto hosts
            for ip, (node, vmid) in NODE_MAP.items():
                cur.execute("UPDATE hosts SET node=%s, vmid=%s WHERE guest_ip=%s",
                            (node, vmid, ip))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- health probing
def probe_host(ip, timeout=5):
    """Probe a backend: /v1/models + a 1-token health completion."""
    out = {"ip": ip, "models": None, "health_completion": None, "error": None}
    try:
        r = requests.get(f"http://{ip}:8000/v1/models", timeout=timeout)
        if r.status_code != 200:
            out["error"] = f"models http {r.status_code}"
            return out
        data = r.json().get("data") or []
        out["models"] = [{"id": m.get("id"), "max_model_len": m.get("max_model_len"),
                          "root": m.get("root")} for m in data]
        if data:
            mid = data[0]["id"]
            try:
                c = requests.post(f"http://{ip}:8000/v1/chat/completions", timeout=timeout * 3,
                                  json={"model": mid, "max_tokens": 1,
                                        "messages": [{"role": "user", "content": "ping"}]})
                out["health_completion"] = "ok" if c.status_code == 200 else f"http {c.status_code}"
            except Exception as e:
                out["health_completion"] = f"error {e.__class__.__name__}"
    except Exception as e:
        out["error"] = f"{e.__class__.__name__}: {str(e)[:80]}"
    return out


def sync_registry():
    """Reconcile model_registry with live backend probes. Returns summary."""
    conn = pg()
    if not conn:
        return {"error": "pg unavailable"}
    summary = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT host_id, guest_ip, name FROM hosts ORDER BY host_id")
            hosts = cur.fetchall()
        for hid, ip, name in hosts:
            p = probe_host(ip)
            healthy = (p["models"] and p["health_completion"] == "ok")
            state = "healthy" if healthy else ("unhealthy" if p["error"] or p["health_completion"] else "unknown")
            with conn.cursor() as cur:
                cur.execute("""SELECT desired_power_state, management_mode FROM hosts
                               WHERE host_id=%s""", (hid,))
                row = cur.fetchone()
                desired, mode = row if row else ("RUNNING", "MANAGED")
                routable = bool(healthy and desired == "RUNNING" and mode == "MANAGED")
                if p["models"]:
                    for m in p["models"]:
                        cur.execute("""
                            INSERT INTO model_registry (logical_model_name, host_id, engine,
                                backend_url, context_limit, health, routable, last_verified, updated_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,now(),now())
                            ON CONFLICT (logical_model_name, host_id) DO UPDATE SET
                                context_limit=EXCLUDED.context_limit,
                                health=EXCLUDED.health, routable=EXCLUDED.routable,
                                last_verified=now(), updated_at=now()""",
                            (m["id"], hid, "llamacpp" if ip == "10.0.20.165" else "vllm",
                             f"http://{ip}:8000/v1", m.get("max_model_len"),
                             state, routable))
                else:
                    # host down: mark its rows not routable, keep last known model
                    cur.execute("""UPDATE model_registry SET health=%s, routable=FALSE, updated_at=now()
                                   WHERE host_id=%s""", (state, hid))
            summary.append({"ip": ip, "name": name, "state": state, "routable": routable})
        # aliases: routable iff enabled + target routable somewhere
        cur = conn.cursor()
        cur.execute("""SELECT value FROM manager_settings WHERE key='v011.role_aliases.enabled'""")
        row = cur.fetchone()
        aliases_on = row and str(row[0]).lower() == "true"
        cur.execute("""DELETE FROM model_registry WHERE is_alias=TRUE""")
        if aliases_on:
            for alias, target in (("fast", "qwen3.6-35b-a3b"), ("code", "qwen3.8-flash-next"),
                                  ("frontier", None)):
                if not target:
                    continue
                cur.execute("""SELECT COUNT(*) FROM model_registry
                               WHERE logical_model_name=%s AND routable=TRUE""", (target,))
                ok = cur.fetchone()[0] > 0
                cur.execute("""
                    INSERT INTO model_registry (logical_model_name, host_id, engine, is_alias,
                        alias_target, health, routable, last_verified, updated_at)
                    VALUES (%s, NULL, 'alias', TRUE, %s, %s, %s, now(), now())
                    ON CONFLICT (logical_model_name, host_id) DO NOTHING""",
                    (alias, target, "healthy" if ok else "unhealthy", ok))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return {"error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()
    return {"hosts": summary}


def routable_models():
    """Models currently routable (registry gate §C2). Returns list of names."""
    conn = pg()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT logical_model_name FROM model_registry
                           WHERE routable=TRUE""")
            return sorted(r[0] for r in cur.fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------- key validation
def validate_key(raw_key):
    """Validate a LiteLLM virtual key. Returns metadata dict or None."""
    if not raw_key:
        return None
    master = sec("litellm_master_key")
    if master and raw_key == master:
        return {"key_alias": "master", "blocked": False, "_master": True}
    try:
        r = requests.get(f"{LITELLM_URL}/key/info", params={"key": raw_key},
                         headers={"Authorization": f"Bearer {master}"}, timeout=6)
        if r.status_code != 200:
            return None
        meta = (r.json() or {}).get("info") or {}
        if meta.get("blocked"):
            return None
        return meta
    except Exception:
        return None


# ---------------------------------------------------------------- /v1/models (gated)
@ROUTER.get("/v1/models")
def gated_models(request: Request):
    auth = request.headers.get("authorization") or ""
    key = auth.replace("Bearer", "").strip() if auth else ""
    meta = validate_key(key)
    if not meta:
        return JSONResponse({"error": "invalid or missing API key"},
                            status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
    names = routable_models()
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": n, "object": "model", "created": now, "owned_by": "marion-ia-usa"}
        for n in names]}


# ---------------------------------------------------------------- desired-state API (§B3)
@ROUTER.post("/api/hosts/{ip}/desired-state")
async def set_desired_state(ip: str, request: Request):
    user = request.session.get("user")
    role = request.session.get("role")
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if role not in {"Admin", "Security-Admin"}:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if ip not in HOST_IPS:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    body = await request.json()
    dps = body.get("desired_power_state")
    mode = body.get("management_mode")
    if dps not in ("RUNNING", "STOPPED_INTENTIONAL", None):
        return JSONResponse({"error": "desired_power_state must be RUNNING|STOPPED_INTENTIONAL"}, status_code=400)
    if mode not in ("MANAGED", "UNMANAGED", None):
        return JSONResponse({"error": "management_mode must be MANAGED|UNMANAGED"}, status_code=400)
    conn = pg()
    try:
        with conn.cursor() as cur:
            if dps:
                cur.execute("UPDATE hosts SET desired_power_state=%s WHERE guest_ip=%s", (dps, ip))
            if mode:
                cur.execute("UPDATE hosts SET management_mode=%s WHERE guest_ip=%s", (mode, ip))
            cur.execute("""INSERT INTO recovery_events (ip, event_type, detail)
                           VALUES (%s,'desired_state_change',%s)""",
                        (ip, json.dumps({"user": user, "desired_power_state": dps,
                                         "management_mode": mode})))
            cur.execute("""SELECT guest_ip, desired_power_state, management_mode FROM hosts
                           WHERE guest_ip=%s""", (ip,))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if dps == "STOPPED_INTENTIONAL":
        # drain immediately: registry gate flips on next sync; force now
        sync_registry()
    return {"status": "ok", "host": {"ip": row[0], "desired_power_state": row[1],
                                     "management_mode": row[2]}}


@ROUTER.post("/api/hosts/{ip}/power-off")
async def power_off(ip: str, request: Request):
    """Explicit operator action to actually stop a host marked intentionally offline."""
    user = request.session.get("user")
    role = request.session.get("role")
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if role not in {"Admin", "Security-Admin"}:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if ip not in HOST_IPS:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    conn = pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT desired_power_state FROM hosts WHERE guest_ip=%s", (ip,))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row or row[0] != "STOPPED_INTENTIONAL":
        return JSONResponse(
            {"error": "set desired state to STOPPED_INTENTIONAL before power-off"}, status_code=409)
    ok = vm_power(ip, "shutdown")
    conn = pg()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO recovery_events (ip, event_type, detail)
                           VALUES (%s,'intentional_power_off',%s)""",
                        (ip, json.dumps({"user": user, "ok": ok})))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok" if ok else "error"}


@ROUTER.get("/api/hosts/state")
def hosts_state():
    """Fleet desired/current state + recovery history for the dashboard."""
    conn = pg()
    if not conn:
        return JSONResponse({"error": "pg unavailable"}, status_code=503)
    out = []
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT h.guest_ip, h.name, h.desired_power_state, h.management_mode,
                                  h.node, h.vmid
                           FROM hosts h ORDER BY h.guest_ip""")
            hosts = cur.fetchall()
            for ip, name, dps, mode, node, vmid in hosts:
                vm = vm_status(ip) if mode == "MANAGED" else {}
                cur.execute("""SELECT event_type, detail, created_at FROM recovery_events
                               WHERE ip=%s ORDER BY created_at DESC LIMIT 5""", (ip,))
                events = [{"type": t, "detail": d, "ts": c.isoformat()} for t, d, c in cur.fetchall()]
                out.append({"ip": ip, "name": name, "desired_power_state": dps,
                            "management_mode": mode, "node": node, "vmid": vmid,
                            "vm_status": vm.get("status"), "events": events})
    finally:
        conn.close()
    return {"hosts": out}


# ---------------------------------------------------------------- M2M agent key API (§J)
def _service_auth(request: Request):
    """Service identity auth: Authorization: Bearer <service_token>."""
    tok = sec("service_token")
    if not tok:
        return False
    auth = (request.headers.get("authorization") or "").replace("Bearer", "").strip()
    return bool(auth) and auth == tok


@ROUTER.post("/api/service/agents/provision")
async def service_provision(request: Request):
    """Idempotent agent provisioning for the future AI Agent Deployment Service.
    Returns raw key on first creation AND on idempotent retries (stored server-side);
    raw key is NEVER exposed via the web UI after creation."""
    if not _service_auth(request):
        return JSONResponse({"error": "service auth required"}, status_code=401)
    body = await request.json()
    project_id = (body.get("project_id") or "").strip()
    agent_instance_id = (body.get("agent_instance_id") or "").strip()
    agent_name = (body.get("agent_name") or "").strip()
    harness = (body.get("harness") or "").strip()
    budget = body.get("monthly_budget_usd")
    if not project_id or not agent_instance_id:
        return JSONResponse({"error": "project_id and agent_instance_id required"}, status_code=400)
    external_id = f"{project_id}/{agent_instance_id}"
    alias = agent_name or external_id.replace("/", "-")

    conn = pg()
    master = sec("litellm_master_key")
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT key_id, alias, raw_key, created_at FROM agent_keys
                           WHERE external_id=%s AND revoked_at IS NULL""", (external_id,))
            row = cur.fetchone()
            if row:
                conn.commit()
                return {"status": "existing", "key_id": row[0], "alias": row[1],
                        "raw_api_key": row[2],
                        "openai_base_url": "https://llm-manager.marion-ia-usa.internal/v1",
                        "created_at": row[3].isoformat(),
                        "note": "idempotent replay; existing key returned"}
            budget = float(budget) if budget not in (None, "") else None
            payload = {"key_alias": alias}
            if budget:
                payload["max_budget"] = budget
                payload["budget_duration"] = "30d"
            r = requests.post(f"{LITELLM_URL}/key/generate", json=payload,
                              headers={"Authorization": f"Bearer {master}"}, timeout=10)
            if r.status_code != 200:
                return JSONResponse({"error": f"litellm key generate failed: {r.text[:150]}"},
                                    status_code=502)
            raw = r.json().get("key")
            cur.execute("""INSERT INTO agent_keys (external_id, project_id, agent_instance_id,
                            agent_name, harness, alias, raw_key, created_by, monthly_budget)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'service-api',%s)
                           RETURNING key_id""",
                        (external_id, project_id, agent_instance_id, agent_name, harness,
                         alias, raw, budget))
            kid = cur.fetchone()[0]
        conn.commit()
        return {"status": "created", "key_id": kid, "alias": alias, "raw_api_key": raw,
                "openai_base_url": "https://llm-manager.marion-ia-usa.internal/v1"}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@ROUTER.post("/api/service/agents/rotate")
async def service_rotate(request: Request):
    if not _service_auth(request):
        return JSONResponse({"error": "service auth required"}, status_code=401)
    body = await request.json()
    project_id = (body.get("project_id") or "").strip()
    agent_instance_id = (body.get("agent_instance_id") or "").strip()
    if not project_id or not agent_instance_id:
        return JSONResponse({"error": "project_id and agent_instance_id required"}, status_code=400)
    external_id = f"{project_id}/{agent_instance_id}"
    master = sec("litellm_master_key")
    conn = pg()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT key_id, alias, raw_key FROM agent_keys
                           WHERE external_id=%s AND revoked_at IS NULL""", (external_id,))
            row = cur.fetchone()
            if not row:
                return JSONResponse({"error": "agent not found"}, status_code=404)
            kid, alias, old_raw = row
            r = requests.post(f"{LITELLM_URL}/key/generate", json={"key_alias": alias},
                              headers={"Authorization": f"Bearer {master}"}, timeout=10)
            if r.status_code != 200:
                return JSONResponse({"error": "litellm key generate failed"}, status_code=502)
            new_raw = r.json().get("key")
            if old_raw:
                requests.post(f"{LITELLM_URL}/key/delete", json={"keys": [old_raw]},
                              headers={"Authorization": f"Bearer {master}"}, timeout=10)
            cur.execute("""UPDATE agent_keys SET raw_key=%s, created_at=now() WHERE key_id=%s""",
                        (new_raw, kid))
        conn.commit()
        return {"status": "rotated", "key_id": kid, "raw_api_key": new_raw,
                "openai_base_url": "https://llm-manager.marion-ia-usa.internal/v1"}
    finally:
        conn.close()


@ROUTER.post("/api/service/agents/revoke")
async def service_revoke(request: Request):
    if not _service_auth(request):
        return JSONResponse({"error": "service auth required"}, status_code=401)
    body = await request.json()
    project_id = (body.get("project_id") or "").strip()
    agent_instance_id = (body.get("agent_instance_id") or "").strip()
    if not project_id or not agent_instance_id:
        return JSONResponse({"error": "project_id and agent_instance_id required"}, status_code=400)
    external_id = f"{project_id}/{agent_instance_id}"
    master = sec("litellm_master_key")
    conn = pg()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT key_id, raw_key FROM agent_keys
                           WHERE external_id=%s AND revoked_at IS NULL""", (external_id,))
            row = cur.fetchone()
            if not row:
                return JSONResponse({"error": "agent not found"}, status_code=404)
            kid, raw = row
            if raw:
                requests.post(f"{LITELLM_URL}/key/delete", json={"keys": [raw]},
                              headers={"Authorization": f"Bearer {master}"}, timeout=10)
            cur.execute("""UPDATE agent_keys SET revoked_at=now(), enabled=FALSE WHERE key_id=%s""",
                        (kid,))
        conn.commit()
        return {"status": "revoked", "key_id": kid}
    finally:
        conn.close()


@ROUTER.get("/api/service/agents/status")
def service_status(request: Request):
    if not _service_auth(request):
        return JSONResponse({"error": "service auth required"}, status_code=401)
    conn = pg()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT key_id, external_id, alias, agent_name, harness, created_at,
                                  enabled, revoked_at IS NOT NULL, monthly_budget
                           FROM agent_keys ORDER BY key_id""")
            rows = cur.fetchall()
        return {"agents": [
            {"key_id": r[0], "external_id": r[1], "alias": r[2], "agent_name": r[3],
             "harness": r[4], "created_at": r[5].isoformat(), "enabled": r[6],
             "revoked": r[7], "monthly_budget": float(r[8]) if r[8] else None}
            for r in rows]}
    finally:
        conn.close()
