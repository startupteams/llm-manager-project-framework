#!/usr/bin/env python3
"""LLM Manager — Phase 3: LDAP auth, API key management, deployment control.

Real data only. No fake metrics. Secrets loaded from /etc/llm-manager/secrets.
"""
import json
import hashlib
import os
import re
import secrets as pysecrets
import subprocess
import time
import concurrent.futures as cf
from datetime import datetime, timezone

import ldap3
import psycopg2
import requests
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

APP_VERSION = "0.11.0-default-baseline"

SECRETS = "/etc/llm-manager/secrets"
VLLM_HOSTS = [
    {"name": "MIAM-00111 / VM103", "ip": "10.0.20.161"},
    {"name": "MIAM-00112 / VM401", "ip": "10.0.20.162"},
    {"name": "MIAM-00143 / VM109", "ip": "10.0.20.163"},
    {"name": "MIAM-00144 / VM111", "ip": "10.0.20.164"},
    {"name": "MIAM-00149 / VM149 (llama.cpp CPU)", "ip": "10.0.20.165"},
]
PG_HOST = "10.0.20.116"
PG_DB = "llmmanager"
PG_USER = "llmmanager"
PG_PW_FILE = f"{SECRETS}/pg_app_creds"
LITELLM_URL = "http://127.0.0.1:4000"
LOGICAL_MODEL = "startupteams/general"
LDAP_HOST = "10.0.20.101"
LDAP_PORT = 3890

# Rate components fallback (DB electricity_rates is authoritative when present)
RATE_WINTER_BASE = 0.09992   # Alliant Rate 400 Sep-May
RATE_SUMMER_BASE = 0.13398   # Alliant Rate 400 Jun-Aug
RATE_FEES_RIDERS = 0.05830   # year-round Emporia-observed adder
SUMMER_MONTHS = {6, 7, 8}

ROLE_ORDER = ["Viewer", "Operator", "Admin", "Security-Admin"]
GROUP_PREFIX = "llm-manager-vm114-"
CAN_WRITE_KEYS = {"Admin", "Security-Admin"}
CAN_EDIT_DEPLOY = {"Admin", "Security-Admin"}
CAN_RESTART = {"Operator", "Admin", "Security-Admin"}

AUDIT_LOG = "/var/log/llm-manager/audit.log"


def sec(name):
    try:
        with open(os.path.join(SECRETS, name)) as f:
            return f.read().strip()
    except Exception:
        return None


SESSION_KEY = sec("session_key") or "dev-insecure-key"
LITELLM_MASTER = sec("litellm_master_key")
LDAP_BIND_DN = sec("ldap_bind_dn") or "uid=svc-llm-manager-bind,ou=people,dc=example,dc=com"
LDAP_BIND_PW = sec("ldap_bind_pw")
LDAP_BASE = sec("ldap_base_dn") or "dc=example,dc=com"

app = FastAPI(title="MARION-IA-USA LLM Manager", version=APP_VERSION)
app.add_middleware(SessionMiddleware, secret_key=SESSION_KEY, max_age=43200)

# ---- v0.11: desired-state, model registry, gated /v1/models, service API ----
try:
    from v011_core import ROUTER as V011_ROUTER, init_db as v011_init_db, sync_registry
    app.include_router(V011_ROUTER)
    V011_ENABLED = True
except Exception as _v011err:  # pragma: no cover
    import logging
    logging.getLogger("uvicorn").error("v0.11 module failed to load: %s", _v011err)
    V011_ENABLED = False


def audit(user, action, target, result, detail=None):
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "user": user, "action": action, "target": target,
            "result": result, "detail": detail,
        }
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- LDAP auth

def ldap_login(username, password):
    """Return (role|None, error|None). Binds as service account, finds user,
    verifies password by binding as the user, maps groups to a role."""
    if not LDAP_BIND_PW:
        return None, "server: bind credentials missing"
    try:
        srv = ldap3.Server(LDAP_HOST, port=LDAP_PORT, get_info=None)
        conn = ldap3.Connection(srv, user=LDAP_BIND_DN, password=LDAP_BIND_PW,
                                auto_bind=True, raise_exceptions=True)
    except Exception as e:
        return None, f"server: service bind failed: {str(e)[:80]}"
    try:
        conn.search(LDAP_BASE, f"(uid={ldap3.utils.conv.escape_filter_chars(username)})",
                    attributes=["displayName"])
        if not conn.entries:
            return None, "unknown user"
        user_dn = conn.entries[0].entry_dn
    finally:
        conn.unbind()
    # map groups: search ou=groups for entries whose member=<user_dn> (LLDAP has no memberOf)
    groups = []
    try:
        gconn = ldap3.Connection(srv, user=LDAP_BIND_DN, password=LDAP_BIND_PW,
                                 auto_bind=True, raise_exceptions=True)
        gconn.search(f"ou=groups,{LDAP_BASE}",
                     f"(member={ldap3.utils.conv.escape_filter_chars(user_dn)})",
                     attributes=["cn"])
        groups = [e.cn.value for e in gconn.entries]
        gconn.unbind()
    except Exception:
        groups = []
    try:
        uconn = ldap3.Connection(srv, user=user_dn, password=password,
                                 auto_bind=True, raise_exceptions=True)
        uconn.unbind()
    except Exception:
        return None, "invalid credentials"
    role = None
    for g in groups:
        m = re.match(rf"{re.escape(GROUP_PREFIX)}(\S+)", g or "")
        if m and m.group(1) in ROLE_ORDER:
            r = m.group(1)
            if role is None or ROLE_ORDER.index(r) > ROLE_ORDER.index(role):
                role = r
    if role is None:
        return None, "no llm-manager role assigned (contact an admin)"
    return role, None


def current_user(request):
    return request.session.get("user"), request.session.get("role")


# ---------------------------------------------------------------- probes

def _load_pg_pw():
    try:
        with open(PG_PW_FILE) as f:
            for line in f:
                if line.startswith("PG_PW="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return None


def probe_vllm(host):
    out = dict(host)
    out["port"] = 8000
    t0 = time.time()
    try:
        s = requests.Session()
        s.headers["Connection"] = "close"
        r = s.get(f"http://{host['ip']}:8000/v1/models", timeout=4)
        latency_ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            data = r.json().get("data", [])
            out.update(
                status="healthy",
                latency_ms=latency_ms,
                model=data[0]["id"] if data else None,
                max_model_len=data[0].get("max_model_len") if data else None,
                root=data[0].get("root") if data else None,
            )
        else:
            out.update(status="http_" + str(r.status_code), latency_ms=latency_ms)
    except Exception as e:
        out.update(status="error", error=str(e)[:80], latency_ms=int((time.time() - t0) * 1000))
    return out


def probe_pg():
    pw = _load_pg_pw()
    if not pw:
        return {"status": "error", "error": "no pg credentials"}
    t0 = time.time()
    try:
        c = psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER, password=pw,
                             connect_timeout=4)
        cur = c.cursor()
        cur.execute("SELECT version()")
        v = cur.fetchone()[0]
        c.close()
        return {"status": "healthy", "latency_ms": int((time.time() - t0) * 1000),
                "version": v.split(",")[0]}
    except Exception as e:
        return {"status": "error", "error": str(e)[:100]}


# ---------------------------------------------------------------- LiteLLM key API

def litellm_req(method, path, payload=None, params=None):
    r = requests.request(method, f"{LITELLM_URL}{path}", json=payload, params=params,
                         headers={"Authorization": f"Bearer {LITELLM_MASTER}"}, timeout=15)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def litellm_post(path, payload=None):
    return litellm_req("POST", path, payload)




# ---------------------------------------------------------------- hosted-command presets (v0.8.0 Phase B)
def _ensure_preset_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS hosting_command_presets (
            id SERIAL PRIMARY KEY,
            scope_type TEXT NOT NULL DEFAULT 'single_host',
            scope_key TEXT NOT NULL,
            engine TEXT NOT NULL DEFAULT 'vllm',
            friendly_name TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            sort_order INT NOT NULL DEFAULT 1000,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            last_used_at TIMESTAMPTZ,
            use_count INT NOT NULL DEFAULT 0,
            deleted_at TIMESTAMPTZ,
            created_by TEXT, updated_by TEXT)""")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_hcp_active_content
            ON hosting_command_presets (scope_type, scope_key, engine, content_hash)
            WHERE deleted_at IS NULL""")
        # per-server RDMA toggle (v0.8.0 Phase C / Jordan 2026-09-12 request)
        cur.execute("""ALTER TABLE rdma_ring_nodes ADD COLUMN IF NOT EXISTS rdma_enabled BOOLEAN NOT NULL DEFAULT TRUE""")
    conn.commit()


def _engine_for_ip(ip):
    if ip == "10.0.20.165":
        return "llamacpp"
    return "vllm"


def _host_name_for_ip(ip):
    for h in VLLM_HOSTS:
        if h["ip"] == ip:
            return h["name"]
    return ip


def _touch_preset(cur, pid, user):
    cur.execute("""UPDATE hosting_command_presets SET last_used_at = now(),
                   use_count = use_count + 1, updated_by = %s,
                   sort_order = COALESCE((SELECT MIN(sort_order)-1 FROM hosting_command_presets
                                          WHERE scope_key = (SELECT scope_key FROM hosting_command_presets WHERE id = %s)
                                            AND deleted_at IS NULL), 0)
                   WHERE id = %s AND deleted_at IS NULL""", (user, pid, pid))

# ---------------------------------------------------------------- routes: health/status

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": APP_VERSION}


@app.get("/api/status")
def api_status(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(probe_vllm, h): h["ip"] for h in VLLM_HOSTS}
        pg = ex.submit(probe_pg)
        hosts = [f.result() for f in cf.as_completed(futs)]
        hosts.sort(key=lambda h: h["ip"])
        pg = pg.result()
    return {"version": APP_VERSION, "hosts": hosts, "postgres": pg, "ts": time.time()}


# ---------------------------------------------------------------- power & cost (Phase 5)

def _pg():
    pw = _load_pg_pw()
    if not pw:
        return None
    try:
        return psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER, password=pw,
                                connect_timeout=4)
    except Exception:
        return None


def effective_rate(month: int):
    """Return (effective_rate_per_kwh, source) using versioned DB components when present,
    else built-in plan constants. Winter = Sep-May, summer = Jun-Aug (plan v0.2.1 §16)."""
    season = "summer" if month in SUMMER_MONTHS else "winter"
    base = RATE_SUMMER_BASE if season == "summer" else RATE_WINTER_BASE
    fees = RATE_FEES_RIDERS
    source = "built-in plan v0.2.1 constants"
    conn = _pg()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT component, value_per_kwh FROM electricity_rates
                    WHERE season = %s
                      AND effective_from <= CURRENT_DATE
                      AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)
                    ORDER BY rate_id DESC""", (season,))
                vals = {r[0]: float(r[1]) for r in cur.fetchall()}
            conn.close()
            if "fees_riders" in vals:
                fees = vals["fees_riders"]
                if season == "summer" and "base_energy_summer" in vals:
                    base = vals["base_energy_summer"]
                elif season == "winter" and "base_energy_winter" in vals:
                    base = vals["base_energy_winter"]
                source = "electricity_rates table (versioned components)"
        except Exception:
            pass
    return base + fees, source


@app.get("/api/power")
def api_power(request: Request, minutes: int = 60):
    """GPU watts per host over the last N minutes (1-min rollup) + current status."""
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    minutes = max(5, min(int(minutes), 1440))
    out = {"hosts": [], "rate": {}, "collector": None}
    now = datetime.now(timezone.utc)
    rate, rate_src = effective_rate(now.month)
    out["rate"] = {
        "effective_per_kwh": round(rate, 5),
        "display_cents": round(rate * 100, 2),
        "season": "summer" if now.month in SUMMER_MONTHS else "winter",
        "source": rate_src,
    }
    conn = _pg()
    if not conn:
        out["error"] = "postgres unavailable"
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT host_id, guest_ip, name FROM hosts ORDER BY guest_ip")
            hosts = cur.fetchall()
            for hid, ip, name in hosts:
                cur.execute("""
                    SELECT ts, gpu_watts_avg, gpu_watts_peak FROM host_power_rollup_1m
                    WHERE host_id = %s AND ts >= now() - (%s || ' minutes')::interval
                    ORDER BY ts""", (hid, minutes))
                series = [{"ts": r[0].isoformat(), "avg": float(r[1]) if r[1] else None,
                           "peak": float(r[2]) if r[2] else None} for r in cur.fetchall()]
                cur.execute("""
                    SELECT gpu_index, util_pct, power_watts FROM gpu_samples
                    WHERE host_id = %s
                    ORDER BY sample_id DESC LIMIT %s""", (hid, 24))
                latest = cur.fetchall()
                # most recent reading per GPU index
                cur_gpus = {}
                for gidx, util, watts in latest:
                    if gidx not in cur_gpus:
                        cur_gpus[gidx] = {"gpu_index": gidx,
                                          "util_pct": float(util) if util is not None else None,
                                          "watts": float(watts) if watts is not None else None}
                cur_total = sum(g["watts"] for g in cur_gpus.values() if g["watts"])
                out["hosts"].append({
                    "host_id": hid, "name": name, "ip": ip,
                    "current_gpu_watts": round(cur_total, 1),
                    "series": series,
                    "gpus": sorted(cur_gpus.values(), key=lambda g: g["gpu_index"]),
                })
            cur.execute("""
                SELECT count(*), max(ts) FROM (
                    SELECT ts FROM gpu_samples WHERE ts > now() - interval '5 minutes'
                ) s""")
            cnt, last = cur.fetchone()
            out["collector"] = {"samples_5m": cnt, "last_ts": last.isoformat() if last else None,
                                "healthy": bool(cnt and cnt > 0)}
    finally:
        conn.close()
    return out


@app.get("/api/cost")
def api_cost(request: Request):
    """Cost rollup: GPU energy kWh over lookback windows at effective rate."""
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    out = {"windows": {}}
    now = datetime.now(timezone.utc)
    rate, rate_src = effective_rate(now.month)
    out["rate"] = {"effective_per_kwh": rate, "source": rate_src}
    conn = _pg()
    if not conn:
        out["error"] = "postgres unavailable"
        return out
    try:
        with conn.cursor() as cur:
            for label, interval in (("24h", "24 hours"), ("7d", "7 days"), ("30d", "30 days")):
                cur.execute("""
                    SELECT COALESCE(SUM(gpu_watts_avg), 0) / 60.0 / 1000.0
                    FROM host_power_rollup_1m
                    WHERE ts >= now() - (%s || ' hours')::interval
                      AND ts < date_trunc('minute', now())""", (label[:-1],))
                kwh = float(cur.fetchone()[0] or 0.0)
                out["windows"][label] = {
                    "gpu_kwh": round(kwh, 3),
                    "gpu_cost_usd": round(kwh * rate, 4),
                }
    finally:
        conn.close()
    fac = _facility_split(minutes=1440)
    if fac:
        out["facility"] = fac
        out["note"] = ("GPU-measured compute energy + Emporia facility view (compute vs cooling split). "
                       "Fixed customer charges excluded per plan §16.2.")
    else:
        out["note"] = ("GPU-measured energy only; non-GPU platform overhead and facility/cooling "
                       "attribution arrive with Phase 6. Fixed customer charges excluded per plan §16.2.")
    return out


def _facility_split(minutes=1440):
    """Role-split facility energy from Emporia tables; None when not deployed/empty."""
    conn = _pg()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('facility_power_samples') IS NOT NULL")
            if not cur.fetchone()[0]:
                return None
            cur.execute("""
                SELECT COALESCE(m.mapped_role,'other') AS role, AVG(s.watts),
                       SUM(s.kwh_interval)
                FROM facility_power_samples s
                LEFT JOIN emporia_channel_map m ON m.device_gid = s.device_gid AND m.channel_num = s.channel_num
                WHERE s.ts >= now() - (%s || ' minutes')::interval
                GROUP BY COALESCE(m.mapped_role,'other')""", (str(minutes),))
            rows = cur.fetchall()
        if not rows:
            return None
        d = {}
        for r, aw, kwh in rows:
            d[r] = {"avg_watts": round(float(aw or 0), 1),
                    "kwh": round(float(kwh), 3) if kwh is not None else None}
        return d
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass



# ---------------------------------------------------------------- Spend accounting (GPU-power-based $/token)
SPEND_IDLE_NON_GPU_WATTS = 120.0   # per-host platform assumption (plan §15.2); refine vs Emporia later
SPEND_CPU_W_PER_UTIL_PT = 0.35     # rough CPU-load component per util % point


@app.get("/api/spend")
def api_spend(request: Request, window: str = "24h"):
    """Per-key + fleet token/energy/cost rollup.

    Energy attribution: fleet GPU watts (host_power_rollup_1m) over the window,
    optionally plus per-host non-GPU platform estimate (server-total mode).
    Token counts: LiteLLM_SpendLogs (litellm DB) in the same window.
    """
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if window not in ("24h", "7d", "30d"):
        window = "24h"
    hours = {"24h": 24, "7d": 168, "30d": 720}[window]
    now = datetime.now(timezone.utc)
    rate, rate_src = effective_rate(now.month)
    out = {"window": window, "rate": {"effective_per_kwh": rate, "source": rate_src},
           "modes": {}, "keys": [], "fleet": {}}
    conn = _pg()
    if not conn:
        out["error"] = "postgres unavailable"
        return out
    lit = None
    try:
        pw = _load_pg_pw()
        lit = psycopg2.connect(host=PG_HOST, dbname="litellm", user=PG_USER, password=pw,
                               connect_timeout=4)
        # ---- tokens per key over window ----
        with lit.cursor() as lcur:
            lcur.execute("""
                SELECT api_key,
                       COUNT(*) AS reqs,
                       COALESCE(SUM(prompt_tokens),0) AS pt,
                       COALESCE(SUM(completion_tokens),0) AS ct
                FROM "LiteLLM_SpendLogs"
                WHERE "startTime" >= now() - (%s || ' hours')::interval
                GROUP BY api_key ORDER BY ct DESC""", (str(hours),))
            key_rows = lcur.fetchall()
            total_pt = sum(r[2] for r in key_rows)
            total_ct = sum(r[3] for r in key_rows)
            total_reqs = sum(r[1] for r in key_rows)
        # ---- energy over same window (GPU-measured) ----
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(gpu_watts_avg),0)/60.0/1000.0
                FROM host_power_rollup_1m
                WHERE ts >= now() - (%s || ' hours')::interval
                  AND ts < date_trunc('minute', now())""", (str(hours),))
            gpu_kwh = float(cur.fetchone()[0] or 0.0)
            # per-host avg gpu watts + cpu util for server-total estimate
            cur.execute("""
                SELECT r.host_id, AVG(r.gpu_watts_avg),
                       COALESCE(AVG(g.util_pct),0)
                FROM host_power_rollup_1m r
                LEFT JOIN gpu_samples g ON g.host_id = r.host_id
                     AND g.ts >= now() - (%s || ' hours')::interval
                WHERE r.ts >= now() - (%s || ' hours')::interval
                GROUP BY r.host_id""", (str(hours), str(hours)))
            host_rows = cur.fetchall()
        n_hosts = len(host_rows) or 4
        non_gpu_w = SPEND_IDLE_NON_GPU_WATTS * n_hosts
        cpu_w = sum(float(r[2] or 0) for r in host_rows) * SPEND_CPU_W_PER_UTIL_PT
        server_kw = (gpu_kwh * 1000.0 + non_gpu_w * hours + cpu_w * n_hosts * hours) / 1000.0
        # server-total watts time = (gpu_kwh*1000 + non_gpu_w*h + cpu_avg_w*n*h)/1000
        # (cpu util averaged across all gpu samples per host; approximation documented)
        out["modes"] = {
            "gpu_attributed": {"kwh": round(gpu_kwh, 3), "usd": round(gpu_kwh * rate, 4)},
            "server_total": {"kwh": round(server_kw, 3), "usd": round(server_kw * rate, 4)},
        }
        # ---- $/1M tokens (both modes) ----
        for mode, kwh in (("gpu_attributed", gpu_kwh), ("server_total", server_kw)):
            om = out["modes"][mode]
            denom = (total_pt + total_ct) / 1e6
            om["cost_per_1m_tokens_usd"] = round(om["usd"] / ((total_pt + total_ct) / 1e6), 4) if denom else None
            out_tok = total_ct / 1e6
            om["usd_per_1m_output_tokens"] = round(om["usd"] / out_tok, 4) if out_tok else None
        # ---- per-key breakdown ----
        for khash, reqs, pt, ct in key_rows:
            share = ((pt + ct) / (total_pt + total_ct)) if (total_pt + total_ct) else 0
            out["keys"].append({
                "api_key": (khash[:8] + "…") if isinstance(khash, str) and len(khash) > 12 else khash,
                "requests": reqs, "prompt_tokens": pt, "completion_tokens": ct,
                "attributed_gpu_kwh": round(gpu_kwh * share, 4),
                "attributed_cost_usd": round(gpu_kwh * rate * share, 4),
                "cost_per_1m_tokens_usd": round((gpu_kwh * rate * share) / ((pt + ct) / 1e6), 4) if (pt + ct) else None,
            })
        out["fleet"] = {"requests": total_reqs, "prompt_tokens": total_pt,
                        "completion_tokens": total_ct, "total_tokens": total_pt + total_ct}
    except Exception as e:
        out["error"] = f"spend rollup failed: {e.__class__.__name__}: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if lit:
            try:
                lit.close()
            except Exception:
                pass
    out["note"] = ("GPU-attributed energy is measured (NVML); server-total adds per-host "
                   f"platform estimate (idle {SPEND_IDLE_NON_GPU_WATTS}W + CPU "
                   f"{SPEND_CPU_W_PER_UTIL_PT} W/util-pt). Token/energy windows align; "
                   "telemetry history starts 2026-09-09, shorter windows may be partial.")
    return out


@app.get("/api/spend/summary")
def api_spend_summary(request: Request):
    """Compact card data for the dashboard."""
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    s = api_spend(request, "24h")
    if s.get("error"):
        return {"error": s["error"]}
    rate, _ = effective_rate(datetime.now(timezone.utc).month)
    g = s["modes"]["gpu_attributed"]
    st = s["modes"]["server_total"]
    return {
        "tokens_24h": s["fleet"].get("total_tokens", 0),
        "requests_24h": s["fleet"].get("requests", 0),
        "energy_kwh_24h": g["kwh"],
        "cost_usd_24h": g["usd"],
        "cost_usd_30d_server_total": None,  # filled by /api/spend 30d when needed
        "per_1m_gpu": g.get("usd_per_1m_tokens_gpu") or g.get("cost_per_1m_tokens_usd"),
        "per_1m_server_total": st.get("usd_per_1m_output_tokens") or st.get("cost_per_1m_tokens_usd"),
        "effective_rate_per_kwh": rate,
        "note": s.get("note"),
    }


# ---------------------------------------------------------------- Spend analytics (OpenRouter-style, v0.6.0)

def _lit_connect():
    pw = _load_pg_pw()
    return psycopg2.connect(host=PG_HOST, dbname="litellm", user=PG_USER, password=pw,
                            connect_timeout=4)


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


@app.get("/api/spend/analytics")
def api_spend_analytics(request: Request, hours: int = 168, from_: str = "", to: str = ""):
    """OpenRouter-style token/cost analytics over LiteLLM_SpendLogs.

    Query: ?hours=N (default 168) or ?from=ISO&to=ISO for an explicit range.
    Returns usage box (Today/This Week/This Month/All Time), per-model
    min/max/avg/totals, per-key totals (alias-resolved), daily totals, and
    GPU-attributed energy cost for the window at the effective seasonal rate.
    """
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    from datetime import timedelta
    lit = None
    try:
        lit = _lit_connect()
        cur = lit.cursor()
        now = datetime.now(timezone.utc)
        if from_:
            dt0 = _parse_dt(from_)
            if not dt0:
                return JSONResponse({"error": "bad from= ISO datetime"}, status_code=400)
            dt1 = _parse_dt(to) or now
            label = f"range {from_} -> {to or 'now'}"
            where = '"startTime" >= %s AND "startTime" <= %s'
            params = [dt0, dt1]
        else:
            hours = hours if 0 < hours <= 24 * 400 else 168
            dt0 = now - timedelta(hours=hours)
            dt1 = now
            label = f"last {hours}h"
            where = '"startTime" >= %s'
            params = [dt0]
        rate, rate_src = effective_rate(now.month)

        cur = lit.cursor()

        def _tot(since):
            cur.execute("""SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0),
                           COALESCE(SUM(completion_tokens),0), COALESCE(SUM(spend),0)
                           FROM "LiteLLM_SpendLogs"
                           WHERE "startTime" >= %s AND (total_tokens>0 OR spend>0)""", (since,))
            r = cur.fetchone()
            return {"requests": r[0], "prompt_tokens": r[1], "completion_tokens": r[2],
                    "spend_usd": round(float(r[3] or 0), 4)}

        cur.execute("""SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0),
                       COALESCE(SUM(completion_tokens),0), COALESCE(SUM(spend),0)
                       FROM "LiteLLM_SpendLogs" WHERE total_tokens>0 OR spend>0""")
        allt = cur.fetchone()

        cur.execute(f"""SELECT model, model_group, api_key, spend, prompt_tokens,
                        completion_tokens, "startTime", request_duration_ms
                        FROM "LiteLLM_SpendLogs"
                        WHERE {where} AND (total_tokens>0 OR spend>0)""", params)
        rows = cur.fetchall()

        cur.execute('SELECT token, key_alias, key_name FROM "LiteLLM_VerificationToken"')
        tok2alias = {}
        for tok, alias, kname in cur.fetchall():
            if not tok:
                continue
            tok2alias[tok] = alias or kname or (tok[:8] + "...")

        def disp_key(k):
            if not k:
                return "(no key)"
            if k == "litellm_proxy_master_key":
                return "master"
            return tok2alias.get(k, k[:8] + "...")

        usage = {
            "today": _tot(datetime(now.year, now.month, now.day)),
            "this_week": _tot(now - timedelta(days=7)),
            "this_month": _tot(datetime(now.year, now.month, 1)),
            "all_time": {"requests": allt[0], "prompt_tokens": allt[1],
                         "completion_tokens": allt[2], "spend_usd": round(float(allt[3]), 4)},
        }

        # ---- per-model min/max/avg/totals ----
        by_model = {}
        for model, mgroup, api_key, spend, pt, ct, stime, dur in rows:
            m = model or mgroup or "(unknown)"
            d = by_model.setdefault(m, {"requests": 0, "spend": 0.0, "pt": 0, "ct": 0,
                                        "spends": [], "durs": []})
            d["requests"] += 1
            d["spend"] += float(spend or 0)
            d["pt"] += int(pt or 0)
            d["ct"] += int(ct or 0)
            if spend is not None:
                d["spends"].append(float(spend))
            if dur is not None:
                d["durs"].append(int(dur))
        models = []
        for m, d in by_model.items():
            models.append({
                "model": m,
                "requests": d["requests"],
                "min_spend_usd": round(min(d["spends"]), 4) if d["spends"] else None,
                "max_spend_usd": round(max(d["spends"]), 4) if d["spends"] else None,
                "avg_spend_usd": round(sum(d["spends"]) / len(d["spends"]), 4) if d["spends"] else None,
                "total_spend_usd": round(d["spend"], 4),
                "prompt_tokens": d["pt"], "completion_tokens": d["ct"],
                "total_tokens": d["pt"] + d["ct"],
                "avg_duration_ms": round(sum(d["durs"]) / len(d["durs"])) if d["durs"] else None,
            })
        models.sort(key=lambda x: (x["total_spend_usd"] or 0), reverse=True)

        # ---- per-key totals ----
        by_key = {}
        for model, mgroup, api_key, spend, pt, ct, stime, dur in rows:
            dk = disp_key(api_key)
            d = by_key.setdefault(dk, {"requests": 0, "pt": 0, "ct": 0, "spend": 0.0})
            d["requests"] += 1
            d["pt"] += int(pt or 0)
            d["ct"] += int(ct or 0)
            d["spend"] += float(spend or 0)
        keys = [{"key": k, "requests": v["requests"], "prompt_tokens": v["pt"],
                 "completion_tokens": v["ct"], "total_tokens": v["pt"] + v["ct"],
                 "spend_usd": round(v["spend"], 4)}
                for k, v in sorted(by_key.items(), key=lambda kv: -kv[1]["spend"])]

        # ---- daily totals ----
        by_day = {}
        for model, mgroup, api_key, spend, pt, ct, stime, dur in rows:
            day = stime.date().isoformat() if stime else "?"
            d = by_day.setdefault(day, {"requests": 0, "pt": 0, "ct": 0, "spend": 0.0})
            d["requests"] += 1
            d["pt"] += int(pt or 0)
            d["ct"] += int(ct or 0)
            d["spend"] += float(spend or 0)
        days = [{"day": k, "requests": v["requests"],
                 "total_tokens": v["pt"] + v["ct"], "spend_usd": round(v["spend"], 4)}
                for k, v in sorted(by_day.items())]

        # ---- GPU-attributed energy cost over the same window ----
        gpu_kwh = 0.0
        coverage_pct = None
        pgl = _pg()
        if pgl:
            try:
                with pgl.cursor() as c2:
                    c2.execute("""SELECT COALESCE(SUM(gpu_watts_avg),0)/60.0/1000.0
                                  FROM host_power_rollup_1m WHERE ts >= %s""", (dt0,))
                    gpu_kwh = float(c2.fetchone()[0] or 0.0)
                    # §H4 telemetry coverage: minutes with samples vs window minutes
                    c2.execute("""SELECT COUNT(DISTINCT date_trunc('minute', ts))
                                  FROM host_power_rollup_1m WHERE ts >= %s""", (dt0,))
                    have_min = c2.fetchone()[0] or 0
                    want_min = max(1, int((dt1 - dt0).total_seconds() // 60))
                    coverage_pct = round(min(100.0, have_min * 100.0 / want_min), 1)
            finally:
                pgl.close()
        energy_usd = round(gpu_kwh * rate, 4)
        win_tokens = sum(m["total_tokens"] for m in models)

        # §H1/I: provider spend vs direct compute cost are DIFFERENT metrics.
        # Local models: provider spend $0.00 actual (no external provider).
        # OpenRouter rows: provider-reported actual spend; local compute n/a.
        for m in models:
            is_local = not (m["model"] or "").startswith("openrouter/")
            m["provider_spend_usd"] = round(m["total_spend_usd"], 4) if not is_local else 0.0
            m["provider_spend_basis"] = "actual" if not is_local else "actual ($0 — local)"
            m["direct_compute"] = "attributed" if is_local else "n/a"

        out = {
            "range": label,
            "rate": {"effective_per_kwh": rate, "source": rate_src,
                     "components": "winter/summer base + year-round fees/riders (versioned in DB)"},
            "usage": usage,
            "models": models,
            "keys": keys,
            "days": days,
            "window_energy": {
                "gpu_kwh": round(gpu_kwh, 3),
                "usd": energy_usd,
                "usd_per_1m_tokens": (round(energy_usd / (win_tokens / 1e6), 4) if win_tokens else None),
                "telemetry_coverage_pct": coverage_pct,
                "basis": "GPU-measured (NVML) direct compute energy",
            },
            "note": ("Provider spend and local compute cost are separate metrics. "
                     "Local model provider spend is $0 while electricity cost is non-zero. "
                     "Energy figures require telemetry; coverage <100% means partial data."),
        }
        out["window_energy"]["usd_per_1m_tokens"] = (
            round(energy_usd / (win_tokens / 1e6), 4) if win_tokens else None)
        return out
    except Exception as e:
        return JSONResponse({"error": f"analytics failed: {e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        if lit:
            try:
                lit.close()
            except Exception:
                pass


# ---------------------------------------------------------------- Facility telemetry (Phase 6, Emporia)

@app.get("/api/facility")
def api_facility(request: Request, minutes: int = 1440):
    """Emporia facility telemetry: per-channel watts/kWh + compute/cooling/other split."""
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if minutes not in (60, 360, 1440, 10080, 43200):
        minutes = 1440
    out = {"error": None, "channels": [], "roles": {}, "note": ""}
    conn = _pg()
    if not conn:
        out["error"] = "postgres unavailable"
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('facility_power_samples') IS NOT NULL")
            if not cur.fetchone()[0]:
                out["note"] = "Emporia collector not deployed yet (facility_power_samples missing)."
                return out
            cur.execute("""
                SELECT s.device_gid, s.channel_num, m.mapped_role, COALESCE(m.mapped_target,''),
                       AVG(s.watts),
                       SUM(s.kwh_interval)
                FROM facility_power_samples s
                LEFT JOIN emporia_channel_map m ON m.device_gid = s.device_gid AND m.channel_num = s.channel_num
                WHERE s.ts >= now() - (%s || ' minutes')::interval
                GROUP BY s.device_gid, s.channel_num, m.mapped_role, m.mapped_target
                ORDER BY 4 DESC""", (str(minutes),))
            rows = cur.fetchall()
        roles = {}
        for gid, cnum, role_, target, avg_w, kwh in rows:
            kwh = float(kwh) if kwh is not None else 0.0
            ch = {"channel_num": cnum, "role": role_ or "other", "target": target,
                  "avg_watts": round(float(avg_w or 0), 1), "kwh": round(kwh, 3)}
            out["channels"].append(ch)
            r = ch["role"]
            roles.setdefault(r, {"avg_watts": 0.0, "kwh": 0.0})
            roles[r]["avg_watts"] += ch["avg_watts"]
            roles[r]["kwh"] += ch["kwh"]
        out["roles"] = roles
        out["note"] = (f"Emporia facility view, last {minutes} min. Role mapping per "
                       "emporia_channel_map (mini-split = cooling; PDU channels = compute).")
        return out
    except Exception as e:
        out["error"] = f"facility query failed: {e.__class__.__name__}: {e}"
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- Distributed / RDMA ring (plan §19, v0.7.0)

RDMA_RING_DEFAULTS = [
    {"host": "MIAM-00111", "ip": "10.0.20.111", "vmid": 103, "guest_ip": "10.0.20.161"},
    {"host": "MIAM-00143", "ip": "10.0.20.143", "vmid": 109, "guest_ip": "10.0.20.163"},
    {"host": "MIAM-00144", "ip": "10.0.20.144", "vmid": 111, "guest_ip": "10.0.20.164"},
]


def _ensure_rdma_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS rdma_ring_nodes (
            host TEXT PRIMARY KEY, ip TEXT, vmid INT, guest_ip TEXT,
            nic_model TEXT, link_state TEXT, qualified BOOLEAN DEFAULT FALSE,
            evidence JSONB, notes TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS multi_node_deployments (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, model TEXT NOT NULL,
            participating_hosts JSONB NOT NULL, topology JSONB,
            state TEXT DEFAULT 'planned', notes TEXT,
            created_at TIMESTAMPTZ DEFAULT now())""")
        cur.execute("""INSERT INTO rdma_ring_nodes (host, ip, vmid, guest_ip, nic_model, notes)
                       SELECT d.host, d.ip, d.vmid, d.guest_ip, 'Mellanox ConnectX-5 Ex', 'seeded from RDMA-0 inventory'
                       FROM (VALUES (%s,%s,%s,%s),(%s,%s,%s,%s),(%s,%s,%s,%s)) AS d(host,ip,vmid,guest_ip)
                       WHERE NOT EXISTS (SELECT 1 FROM rdma_ring_nodes)""",
                    (RDMA_RING_DEFAULTS[0]["host"], RDMA_RING_DEFAULTS[0]["ip"], RDMA_RING_DEFAULTS[0]["vmid"], RDMA_RING_DEFAULTS[0]["guest_ip"],
                     RDMA_RING_DEFAULTS[1]["host"], RDMA_RING_DEFAULTS[1]["ip"], RDMA_RING_DEFAULTS[1]["vmid"], RDMA_RING_DEFAULTS[1]["guest_ip"],
                     RDMA_RING_DEFAULTS[2]["host"], RDMA_RING_DEFAULTS[2]["ip"], RDMA_RING_DEFAULTS[2]["vmid"], RDMA_RING_DEFAULTS[2]["guest_ip"]))
    conn.commit()


@app.get("/api/rdma/ring")
def api_rdma_ring(request: Request):
    """Ring node inventory + qualification state."""
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_rdma_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT host, ip, vmid, guest_ip, nic_model, link_state, qualified,
                           evidence, notes, COALESCE(rdma_enabled, TRUE) FROM rdma_ring_nodes ORDER BY host""")
            rows = cur.fetchall()
        nodes = [{"host": r[0], "ip": r[1], "vmid": r[2], "guest_ip": r[3],
                  "nic_model": r[4], "link_state": r[5], "qualified": r[6],
                  "evidence": r[7], "notes": r[8], "rdma_enabled": r[9]} for r in rows]
        return {"nodes": nodes}
    except Exception as e:
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/rdma/ring")
async def api_rdma_ring_update(request: Request):
    """Update a ring node's qualification state / evidence (Admin+)."""
    user, role = current_user(request)
    if not user or role not in ("Admin", "Security-Admin"):
        return JSONResponse({"error": "Admin role required"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    host = body.get("host")
    if not host:
        return JSONResponse({"error": "host required"}, status_code=400)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_rdma_tables(conn)
        sets, params = [], []
        for col in ("nic_model", "link_state", "qualified", "notes"):
            if col in body:
                sets.append(f"{col} = %s")
                params.append(body[col])
        if "evidence" in body:
            sets.append("evidence = %s::jsonb")
            params.append(json.dumps(body["evidence"]) if not isinstance(body["evidence"], str) else body["evidence"])
        if not sets:
            return JSONResponse({"error": "nothing to update"}, status_code=400)
        params.append(host)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE rdma_ring_nodes SET {', '.join(sets)} WHERE host = %s", params)
        conn.commit()
        return {"status": "ok", "host": host}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.get("/api/rdma/deployments")
def api_rdma_deployments(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_rdma_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, name, model, participating_hosts, topology, state,
                           created_at, notes FROM multi_node_deployments ORDER BY created_at DESC""")
            rows = cur.fetchall()
        deps = [{"id": r[0], "name": r[1], "model": r[2], "participating_hosts": r[3],
                 "topology": r[4], "state": r[5], "created_at": str(r[6]), "notes": r[7]}
                for r in rows]
        return {"deployments": deps}
    except Exception as e:
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/rdma/deployments")
async def api_rdma_deploy_create(request: Request):
    """Create a multi-node vLLM deployment config (plan §19.6-19.8).

    Records the deployment and generates the Ray head/worker + vllm serve +
    NCCL launch configuration. HARD GATE (§19.8): state starts at 'planned'
    and routable=false while any participating host is unqualified. This
    endpoint never touches the gateway or any host.
    """
    user, role = current_user(request)
    if not user or role not in ("Admin", "Security-Admin"):
        return JSONResponse({"error": "Admin role required"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    name = (body.get("name") or "").strip()
    model = (body.get("model") or "").strip()
    hosts = body.get("participating_hosts") or []
    try:
        tp = int(body.get("tp") or 1)
        pp = int(body.get("pp") or 1)
    except Exception:
        return JSONResponse({"error": "tp/pp must be integers"}, status_code=400)
    if not name or not model or len(hosts) < 2:
        return JSONResponse({"error": "name, model, and >=2 participating_hosts required"}, status_code=400)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_rdma_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT host, qualified FROM rdma_ring_nodes WHERE host = ANY(%s)", (hosts,))
            qrows = cur.fetchall()
            qmap = {r[0]: bool(r[1]) for r in qrows}
            gate_errors = []
            emap = {}
            try:
                cur.execute("SELECT host, rdma_enabled FROM rdma_ring_nodes WHERE host = ANY(%s)", (hosts,))
                emap = {r[0]: bool(r[1]) for r in cur.fetchall()}
            except Exception:
                pass
            for h in hosts:
                if h not in qmap:
                    gate_errors.append(f"{h}: not registered in ring")
                elif not qmap[h]:
                    gate_errors.append(f"{h}: not yet RDMA-qualified (plan §19.8)")
                elif not emap.get(h, True):
                    gate_errors.append(f"{h}: RDMA disabled by administrator (v0.8.0 per-server toggle)")
            cur.execute("""INSERT INTO multi_node_deployments
                           (name, model, participating_hosts, topology, state, notes)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (name, model, json.dumps(hosts),
                         json.dumps({"tp": tp, "pp": pp, "dp": 1}),
                         "planned", body.get("notes") or ""))
            dep_id = cur.fetchone()[0]
        conn.commit()
        config = _render_multi_node_config(name, model, hosts, tp, pp)
        return {"id": dep_id, "state": "planned", "routable": False,
                "gate_errors": gate_errors, "config": config,
                "gate_checklist": [
                    "RDMA-1: iperf3 + verbs bandwidth qualified on every ring link",
                    "RDMA-2: NCCL transport NET/IB/GDRDMA proven from inside guests",
                    "RDMA-3: multi-node vLLM functional test with a small model",
                    "RDMA-4: target model qualification (context + concurrency)",
                    "failure behavior + node-loss test recorded",
                    "then set participating hosts qualified=true and flip state",
                ]}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()




# ---------------------------------------------------------------- RDMA per-server toggle (v0.8.0 Phase C)

@app.post("/api/rdma/ring/{host}/toggle")
async def api_rdma_toggle(host: str, request: Request):
    """Per-server RDMA enable/disable (Admin+). Gating control for distributed hosting;
    the physical L2 ring itself is always on and unaffected."""
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if role not in ("Admin", "Security-Admin"):
        return JSONResponse({"error": "Admin role required"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    want = body.get("rdma_enabled")
    if not isinstance(want, bool):
        return JSONResponse({"error": "rdma_enabled must be a boolean"}, status_code=400)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_rdma_tables(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE rdma_ring_nodes SET rdma_enabled = %s, notes = COALESCE(notes, '') WHERE host = %s",
                        (want, host))
            if cur.rowcount == 0:
                return JSONResponse({"error": "unknown ring host"}, status_code=404)
        conn.commit()
        audit(user, "rdma_toggled", host, "ok", {"rdma_enabled": want})
        return {"status": "ok", "host": host, "rdma_enabled": want}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


def _render_multi_node_config(name, model, hosts, tp, pp):
    """Render Ray head/worker + vllm serve launch configuration (plan §19.6)."""
    head = hosts[0]
    workers = hosts[1:]
    head_ip = next((n["ip"] for n in RDMA_RING_DEFAULTS if n["host"] == head), head)
    lines = []
    lines.append("# Multi-node vLLM launch config (plan §19.6 pattern) - NOT yet qualified")
    lines.append(f"# deployment: {name} | model: {model}")
    lines.append(f"# head: {head} ({head_ip})   workers: {', '.join(workers) if workers else '(none - add hosts)'}")
    lines.append(f"# topology: TP={tp} PP={pp} (validate per-rank VRAM before launch, §19.7)")
    lines.append("")
    lines.append(f"# 1) On head ({head}):")
    lines.append("ray start --head --port=6379 --dashboard-host=0.0.0.0")
    lines.append("")
    for w in workers:
        lines.append(f"# 2) On worker ({w}):")
        lines.append(f"ray start --address={head_ip}:6379")
        lines.append("")
    lines.append("# 3) On head - launch vLLM across the cluster:")
    lines.append("export NCCL_DEBUG=INFO")
    lines.append(f"vllm serve {model} \\")
    lines.append(f"    --tensor-parallel-size {tp} \\")
    lines.append(f"    --pipeline-parallel-size {pp} \\")
    lines.append(f"    --distributed-executor-backend ray \\")
    lines.append(f"    --port 8000 --host 0.0.0.0")
    lines.append("")
    lines.append("# 4) Acceptance: NCCL output must show NET/IB/GDRDMA (sockets = NOT RDMA, §19.5)")
    lines.append("# 5) Only after §19.8 checklist completes may this deployment enter routing.")
    return "\n".join(lines)


# ---------------------------------------------------------------- OpenRouter fallback config (Phase 12 pre-work)

def _setting(key, default=None):
    conn = _pg()
    if not conn:
        return default
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM manager_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    finally:
        conn.close()


def _set_setting(key, value, by):
    conn = _pg()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO manager_settings (key, value, updated_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                    updated_at = now(), updated_by = EXCLUDED.updated_by""", (key, value, by))
        conn.commit()
        return True
    finally:
        conn.close()


def _fallback_state():
    """Evaluate caps state per plan §9.3 checklist. Returns dict for UI + API."""
    def _f(k):
        try:
            return float(_setting(k) or 0)
        except (TypeError, ValueError):
            return 0.0
    daily = _f("openrouter.daily_cap_usd")
    monthly = _f("openrouter.monthly_cap_usd")
    warn = _f("openrouter.warning_threshold_usd")
    key = _setting("openrouter.api_key") or ""
    state = {
        "key_present": bool(key),
        "daily_cap": daily,
        "monthly_cap": monthly,
        "warning_threshold": warn,
        "caps_configured": bool(daily > 0 and monthly > 0 and warn > 0 and warn <= daily),
        "fallback_enabled": False,
        "gate_errors": [],
    }
    # HARD GATE (plan §9.3/§23.7): fallback cannot be enabled without caps + key
    if not state["key_present"]:
        state["gate_errors"].append("OpenRouter API key not configured")
    if daily <= 0:
        state["gate_errors"].append("global daily spend cap not configured")
    if monthly <= 0:
        state["gate_errors"].append("global monthly spend cap not configured")
    if warn <= 0:
        state["gate_errors"].append("warning threshold not configured")
    if state["caps_configured"] and state["key_present"]:
        state["fallback_enabled"] = _setting("openrouter.fallback_enabled") == "true"
    return state


@app.get("/api/openrouter")
def api_openrouter_get(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    state = _fallback_state()
    # never return the raw key; mask it
    state["key_masked"] = (state.get("key_present") and
                           ("" if not state["key_present"] else "sk-or-…" + _setting("openrouter.api_key")[-4:])) or None
    state.pop("key_present", None)
    return state


@app.post("/api/openrouter")
async def api_openrouter_set(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if role not in CAN_WRITE_KEYS:  # Admin/Security-Admin only (provider master secret)
        return JSONResponse({"error": "role does not permit provider master configuration"}, status_code=403)
    body = await request.json()
    results = {}
    if "api_key" in body:
        val = (body.get("api_key") or "").strip()
        if val == "":  # empty = clear
            _set_setting("openrouter.api_key", "", user)
            results["api_key"] = "cleared"
        elif val.startswith("sk-or"):
            _set_setting("openrouter.api_key", val, user)
            results["api_key"] = "saved (masked in UI)"
            audit(user, "openrouter_key_saved", "manager_settings", "ok")
        else:
            return JSONResponse({"error": "key must start with 'sk-or' (or be empty to clear)"}, status_code=400)
    for field, dbkey in (("daily_cap_usd", "openrouter.daily_cap_usd"),
                         ("monthly_cap_usd", "openrouter.monthly_cap_usd"),
                         ("warning_threshold_usd", "openrouter.warning_threshold_usd")):
        if field in body:
            try:
                v = float(body[field])
                if v < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return JSONResponse({"error": f"{field} must be a non-negative number"}, status_code=400)
            _set_setting(dbkey, repr(v), user)
            results[field] = v
    if "enable_fallback" in body:
        want = bool(body["enable_fallback"])
        state = _fallback_state()
        if want and not (state["caps_configured"] and state["key_present"]):
            # HARD GATE — do not silently allow uncapped external spend (plan §9.3, §23.7)
            audit(user, "openrouter_fallback_enable_attempt", "manager_settings", "denied",
                  state["gate_errors"])
            return JSONResponse({"error": "fallback blocked by caps gate",
                                 "gate_errors": state["gate_errors"]}, status_code=409)
        _set_setting("openrouter.fallback_enabled", "true" if want else "false", user)
        results["fallback_enabled"] = want
        audit(user, "openrouter_fallback_" + ("enabled" if want else "disabled"),
              "manager_settings", "ok", {"caps": {"daily": state["daily_cap"],
                                                  "monthly": state["monthly_cap"]}})
    return {"saved": results, "state": _fallback_state()}

# ---------------------------------------------------------------- routes: auth

LOGIN_PAGE = """<!doctype html><html><head><title>LLM Manager — Login</title><style>
body{{background:#0d1117;color:#e6edf3;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:2rem;width:320px}}
h1{{font-size:1.1rem;margin:0 0 1rem}} input{{width:100%;box-sizing:border-box;margin:.4rem 0;padding:.55rem;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px}}
button{{width:100%;padding:.6rem;margin-top:.6rem;background:#238636;border:0;color:#fff;border-radius:6px;cursor:pointer;font-weight:600}}
.err{{color:#f85149;font-size:.85rem;margin-top:.6rem;white-space:pre-wrap}}
.sub{{color:#8b949e;font-size:.75rem;margin-top:1rem}}
</style></head><body><div class="card"><h1>🔐 LLM Manager</h1>
<form method="post" action="/login">
<input name="username" placeholder="username" autofocus required>
<input name="password" type="password" placeholder="password" required>
<button>Login</button>{err}
</form><div class="sub">{ver} · LDAP: {ldap_host}</div></div></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    user, _ = current_user(request)
    if user:
        return RedirectResponse("/admin", status_code=302)
    return LOGIN_PAGE.format(err="", ver=APP_VERSION, ldap_host=LDAP_HOST)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    role, err = ldap_login(username.strip(), password)
    if err:
        audit(username, "login", "web", "denied", err)
        return HTMLResponse(LOGIN_PAGE.format(err=f'<div class="err">{err}</div>',
                                              ver=APP_VERSION, ldap_host=LDAP_HOST), status_code=401)
    request.session["user"] = username.strip()
    request.session["role"] = role
    audit(username, "login", "web", "ok", role)
    return RedirectResponse("/admin", status_code=302)


@app.get("/logout")
def logout(request: Request):
    u, _ = current_user(request)
    audit(u or "?", "logout", "web", "ok")
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------- routes: keys

@app.get("/api/keys")
def api_keys(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    st, data = litellm_req("GET", "/key/list")
    tokens = (data or {}).get("keys", []) if isinstance(data, dict) else []
    keys = []
    for tok in tokens if isinstance(tokens, list) else []:
        if not isinstance(tok, str):
            tok = (tok or {}).get("token", "")
        if not tok:
            continue
        ist, info = litellm_req("GET", "/key/info", params={"key": tok})
        meta = {}
        if ist == 200 and isinstance(info, dict):
            meta = (info.get("info") or {}) if isinstance(info.get("info"), dict) else {}
        keys.append({
            "token": tok[:12] + "…",
            # v0.4: alias is the UI-side handle for block/revoke; raw token never leaves server
            "alias": meta.get("key_alias"),
            "spend": meta.get("spend"),
            "max_budget": meta.get("max_budget"),
            "expires": meta.get("expires"),
            "blocked": bool(meta.get("blocked", False)),
            "models": meta.get("models"),
        })
    return {"keys": keys, "lite_status": st}


def resolve_tokens_by_alias(alias):
    """Resolve full LiteLLM token(s) for a UI-supplied alias. The UI only ever
    sees aliases — raw keys are shown once at issuance (plan §9.1)."""
    if not alias:
        return []
    st, data = litellm_req("GET", "/key/list")
    tokens = (data or {}).get("keys", []) if isinstance(data, dict) else []
    found = []
    for tok in tokens if isinstance(tokens, list) else []:
        if not isinstance(tok, str) or not tok:
            continue
        ist, info = litellm_req("GET", "/key/info", params={"key": tok})
        if ist == 200 and isinstance(info, dict):
            meta = (info.get("info") or {}) if isinstance(info.get("info"), dict) else {}
            if meta.get("key_alias") == alias:
                found.append(tok)
    return found


@app.post("/api/keys/issue")
async def api_keys_issue(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if role not in CAN_WRITE_KEYS:
        return JSONResponse({"error": "role does not permit key issuance"}, status_code=403)
    body = await request.json()
    alias = (body.get("alias") or "").strip()
    budget = body.get("budget_usd")
    days = int(body.get("days") or 30)
    if not alias:
        return JSONResponse({"error": "alias required"}, status_code=400)
    payload = {
        "key_alias": alias,
        # v0.11 §E1: agent keys are GENERIC — no model pinning at issue time.
        # Keys can call any currently hosted/routable model via the standard
        # `model` field. Restrictions can be added later if ever needed.
        "duration": f"{days}d",
    }
    if budget not in (None, "", 0):
        try:
            payload["max_budget"] = float(budget)
            payload["budget_duration"] = "30d"
        except ValueError:
            return JSONResponse({"error": "bad budget"}, status_code=400)
    st, data = litellm_post("/key/generate", payload)
    if st == 200 and isinstance(data, dict) and data.get("key"):
        audit(user, "key_issue", alias, "ok", {"days": days, "budget": budget})
        return {"key": data["key"], "alias": alias, "expires": data.get("expires")}
    audit(user, "key_issue", alias, "error", str(data)[:150])
    return JSONResponse({"error": str(data)[:200]}, status_code=502)


@app.post("/api/keys/block")
async def api_keys_block(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if role not in CAN_WRITE_KEYS:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    key = body.get("key")
    unblock = bool(body.get("unblock"))
    # v0.4: 'key' is a UI alias; resolve to real token(s) server-side
    tokens = resolve_tokens_by_alias(key)
    if not tokens:
        audit(user, "key_unblock" if unblock else "key_block", key, "error", "alias not found")
        return JSONResponse({"error": f"no key found for alias {key!r}"}, status_code=404)
    # this LiteLLM build's /key/block & /key/unblock take a SINGULAR {"key": ...};
    # the plural form is accepted-but-ignored (verified 2026-09-09: 401 after singular block)
    results = []
    ok = True
    for tok in tokens:
        st, data = litellm_post("/key/unblock" if unblock else "/key/block", {"key": tok})
        results.append(st)
        ok = ok and st == 200
    audit(user, "key_unblock" if unblock else "key_block", key, "ok" if ok else "error")
    return {"status": 200 if ok else 502, "resolved": len(tokens),
            "result": results}


@app.post("/api/keys/delete")
async def api_keys_delete(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if role not in CAN_WRITE_KEYS:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    key = body.get("key")
    tokens = resolve_tokens_by_alias(key)
    if not tokens:
        audit(user, "key_delete", key, "error", "alias not found")
        return JSONResponse({"error": f"no key found for alias {key!r}"}, status_code=404)
    st, data = litellm_post("/key/delete", {"keys": tokens})
    audit(user, "key_delete", key, "ok" if st == 200 else "error")
    return {"status": st, "resolved": len(tokens), "result": data if st == 200 else str(data)[:150]}


# ---------------------------------------------------------------- routes: deployment control

def run_control(ip, action, extra_stdin=None, timeout=30):
    # v0.4 fix: pass the key PATH, not its contents (v0.3 fed the PEM text to ssh -i,
    # which ssh treated as a filename -> every control-agent call failed)
    key = os.path.join(SECRETS, "keys/id_ed25519")
    if not os.path.exists(key):
        return 1, "no control key configured"
    cmd = ["ssh", "-i", key,
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/root/.ssh/known_hosts_llm",
           "-o", "ConnectTimeout=6", "-o", "BatchMode=yes",
           f"root@{ip}", f"/opt/llm-control/llm-control.sh {action}"]
    try:
        p = subprocess.run(cmd, input=extra_stdin, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return 1, f"timeout after {timeout}s"


@app.get("/api/deployments")
def api_deployments(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    out = []
    def one(h):
        rc, unit = run_control(h["ip"], "get-unit")
        st, stat = run_control(h["ip"], "status")
        return {"ip": h["ip"], "name": h["name"], "unit": unit if rc == 0 else None,
                "status": stat.strip()[:200] if rc == 0 else f"control-error: {stat[:80]}"}
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        out = list(ex.map(one, VLLM_HOSTS))
    return {"deployments": out}


@app.post("/api/deployments/{ip}/unit")
async def api_deploy_unit(ip: str, request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if role not in CAN_EDIT_DEPLOY:
        return JSONResponse({"error": "role does not permit deployment edits"}, status_code=403)
    if ip not in [h["ip"] for h in VLLM_HOSTS]:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    body = await request.json()
    unit = body.get("unit") or ""
    if "ExecStart=" not in unit or "[Service]" not in unit:
        return JSONResponse({"error": "unit text must contain [Service] and ExecStart="}, status_code=400)
    rc, out = run_control(ip, "set-unit", extra_stdin=unit, timeout=45)
    audit(user, "deploy_set_unit", ip, "ok" if rc == 0 else "error", out[-200:])
    return {"status": "ok" if rc == 0 else "error", "output": out[-600:]}


@app.post("/api/deployments/{ip}/restart")
async def api_deploy_restart(ip: str, request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if role not in CAN_RESTART:
        return JSONResponse({"error": "role does not permit restarts"}, status_code=403)
    if ip not in [h["ip"] for h in VLLM_HOSTS]:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    rc, out = run_control(ip, "restart", timeout=90)
    audit(user, "deploy_restart", ip, "ok" if rc == 0 else "error", out[-200:])
    return {"status": "ok" if rc == 0 else "error", "output": out[-600:]}


@app.get("/api/deployments/{ip}/logs")
def api_deploy_logs(ip: str, request: Request, lines: int = 80):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if ip not in [h["ip"] for h in VLLM_HOSTS]:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    lines = max(10, min(int(lines), 400))
    rc, out = run_control(ip, "logs", extra_stdin=None, timeout=30)
    return {"ip": ip, "logs": out[-6000:]}




# ---------------------------------------------------------------- routes: hosted-command presets (v0.8.0 Phase B)

@app.get("/api/hosts/{ip}/presets")
def api_presets_list(ip: str, request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if ip not in [h["ip"] for h in VLLM_HOSTS]:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_preset_tables(conn)
        engine = _engine_for_ip(ip)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, friendly_name, content, sort_order, created_at,
                           last_used_at, use_count, created_by FROM hosting_command_presets
                           WHERE scope_type = 'single_host' AND scope_key = %s AND engine = %s
                             AND deleted_at IS NULL
                           ORDER BY sort_order ASC, created_at ASC""", (ip, engine))
            rows = cur.fetchall()
            presets = [{"id": r[0], "friendly_name": r[1], "content": r[2], "sort_order": r[3],
                        "created_at": str(r[4]), "last_used_at": str(r[5]) if r[5] else None,
                        "use_count": r[6], "created_by": r[7]} for r in rows]
            # first-open import of the active unit (content-hash dedups on later opens)
            if not presets:
                rc, unit = run_control(ip, "get-unit")
                if rc == 0 and unit and "ExecStart=" in unit:
                    text = unit.replace("\r\n", "\n").rstrip()
                    chash = hashlib.sha256(text.encode()).hexdigest()
                    cur.execute("""INSERT INTO hosting_command_presets
                        (scope_type, scope_key, engine, friendly_name, content, content_hash, sort_order, created_by)
                        VALUES ('single_host', %s, %s, %s, %s, %s, 0, %s)
                        ON CONFLICT (scope_type, scope_key, engine, content_hash) WHERE deleted_at IS NULL
                        DO NOTHING RETURNING id""",
                        (ip, engine, f"Imported active command - {time.strftime('%Y-%m-%d')}", text, chash, user))
                    conn.commit()
                    if cur.fetchone():
                        audit(user, "preset_import_active", ip, "ok")
                    cur.execute("""SELECT id, friendly_name, content, sort_order, created_at,
                                   last_used_at, use_count, created_by FROM hosting_command_presets
                                   WHERE scope_type='single_host' AND scope_key=%s AND engine=%s AND deleted_at IS NULL
                                   ORDER BY sort_order ASC, created_at ASC""", (ip, engine))
                    presets = [{"id": r[0], "friendly_name": r[1], "content": r[2], "sort_order": r[3],
                                "created_at": str(r[4]), "last_used_at": str(r[5]) if r[5] else None,
                                "use_count": r[6], "created_by": r[7]} for r in cur.fetchall()]
        return {"presets": presets, "engine": engine}
    except Exception as e:
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/hosts/{ip}/presets")
async def api_presets_create(ip: str, request: Request):
    user, role = current_user(request)
    if not user or role not in CAN_EDIT_DEPLOY:
        return JSONResponse({"error": "role does not permit deployment edits"}, status_code=403)
    if ip not in [h["ip"] for h in VLLM_HOSTS]:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    content = (body.get("content") or "").replace("\r\n", "\n").rstrip()
    name = (body.get("friendly_name") or "").strip() or f"Command - {time.strftime('%Y-%m-%d %H:%M')}"
    if "ExecStart=" not in content or "[Service]" not in content:
        return JSONResponse({"error": "unit text must contain [Service] and ExecStart="}, status_code=400)
    chash = hashlib.sha256(content.encode()).hexdigest()
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_preset_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM hosting_command_presets
                WHERE scope_type='single_host' AND scope_key=%s AND engine=%s
                  AND content_hash=%s AND deleted_at IS NULL""", (ip, _engine_for_ip(ip), chash))
            row = cur.fetchone()
            if row:
                # identical content already saved -> reuse (no duplicate), update name if provided
                if body.get("friendly_name"):
                    cur.execute("UPDATE hosting_command_presets SET friendly_name=%s, updated_by=%s, updated_at=now() WHERE id=%s",
                                (name, user, row[0]))
                _touch_preset(cur, row[0], user)
                conn.commit()
                audit(user, "preset_reuse_dedup", ip, "ok", {"preset_id": row[0]})
                return {"status": "reused", "id": row[0]}
            cur.execute("""SELECT COALESCE(MIN(sort_order),0)-1 FROM hosting_command_presets
                           WHERE scope_key=%s AND deleted_at IS NULL""", (ip,))
            top = cur.fetchone()[0]
            cur.execute("""INSERT INTO hosting_command_presets
                (scope_type, scope_key, engine, friendly_name, content, content_hash, sort_order, created_by, last_used_at, use_count)
                VALUES ('single_host', %s, %s, %s, %s, %s, %s, %s, now(), 1) RETURNING id""",
                (ip, _engine_for_ip(ip), name, content, chash, top, user))
            pid = cur.fetchone()[0]
        conn.commit()
        audit(user, "preset_created", ip, "ok", {"preset_id": pid})
        return {"status": "created", "id": pid}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/hosts/{ip}/presets/{pid}/use")
def api_presets_use(ip: str, pid: int, request: Request):
    user, role = current_user(request)
    if not user or role not in CAN_EDIT_DEPLOY:
        return JSONResponse({"error": "role does not permit deployment edits"}, status_code=403)
    if ip not in [h["ip"] for h in VLLM_HOSTS]:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_preset_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, content FROM hosting_command_presets
                           WHERE id=%s AND scope_key=%s AND deleted_at IS NULL""", (pid, ip))
            row = cur.fetchone()
            if not row:
                return JSONResponse({"error": "preset not found"}, status_code=404)
            _touch_preset(cur, pid, user)
        conn.commit()
        audit(user, "preset_used", ip, "ok", {"preset_id": pid})
        return {"status": "ok", "content": row[1]}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/hosts/{ip}/presets/{pid}/rename")
async def api_presets_rename(ip: str, pid: int, request: Request):
    user, role = current_user(request)
    if not user or role not in CAN_EDIT_DEPLOY:
        return JSONResponse({"error": "role does not permit deployment edits"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    name = (body.get("friendly_name") or "").strip()
    if not name:
        return JSONResponse({"error": "friendly_name required"}, status_code=400)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_preset_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""UPDATE hosting_command_presets SET friendly_name=%s, updated_by=%s, updated_at=now()
                           WHERE id=%s AND scope_key=%s AND deleted_at IS NULL""", (name, user, pid, ip))
            if cur.rowcount == 0:
                return JSONResponse({"error": "preset not found"}, status_code=404)
        conn.commit()
        audit(user, "preset_renamed", ip, "ok", {"preset_id": pid})
        return {"status": "ok"}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/hosts/{ip}/presets/{pid}/delete")
def api_presets_delete(ip: str, pid: int, request: Request):
    user, role = current_user(request)
    if not user or role not in CAN_EDIT_DEPLOY:
        return JSONResponse({"error": "role does not permit deployment edits"}, status_code=403)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_preset_tables(conn)
        with conn.cursor() as cur:
            # soft delete only - never touches the running service
            cur.execute("""UPDATE hosting_command_presets SET deleted_at=now(), updated_by=%s
                           WHERE id=%s AND scope_key=%s AND deleted_at IS NULL""", (user, pid, ip))
            if cur.rowcount == 0:
                return JSONResponse({"error": "preset not found"}, status_code=404)
        conn.commit()
        audit(user, "preset_deleted", ip, "ok", {"preset_id": pid})
        return {"status": "ok"}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/hosts/{ip}/presets/reorder")
async def api_presets_reorder(ip: str, request: Request):
    user, role = current_user(request)
    if not user or role not in CAN_EDIT_DEPLOY:
        return JSONResponse({"error": "role does not permit deployment edits"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    order = body.get("order") or []
    if not order:
        return JSONResponse({"error": "order list required"}, status_code=400)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_preset_tables(conn)
        with conn.cursor() as cur:
            for i, pid in enumerate(order):
                cur.execute("""UPDATE hosting_command_presets SET sort_order=%s, updated_by=%s, updated_at=now()
                               WHERE id=%s AND scope_key=%s AND deleted_at IS NULL""", (i, user, pid, ip))
        conn.commit()
        audit(user, "preset_reordered", ip, "ok", {"count": len(order)})
        return {"status": "ok"}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()

# ---------------------------------------------------------------- deployment history + benchmarks (v5 2026-09-23)

def _ensure_history_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS deployment_revisions (
          revision_id BIGSERIAL PRIMARY KEY,
          revision_label TEXT UNIQUE NOT NULL,
          model TEXT NOT NULL,
          artifact TEXT, artifact_revision TEXT, quantization TEXT,
          runtime TEXT, runtime_version TEXT, host TEXT, vm TEXT, guest_ip TEXT,
          cpu_cores INT, ram_gb NUMERIC, ram_speed TEXT, gpu_model TEXT, gpu_count INT,
          context INT, parallelism TEXT, max_num_seqs INT, kv_cache TEXT,
          launch_command TEXT, systemd_unit TEXT, aliases TEXT,
          status TEXT DEFAULT 'CANDIDATE', visible_by_default BOOLEAN DEFAULT TRUE,
          preset_id INT, notes TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS deployment_revision_id BIGINT")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS prompt_tokens INT")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS output_tokens INT")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS aggregate_output_tok_s NUMERIC")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS ram_used_gb NUMERIC")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS vram_used_gb NUMERIC")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS tested_context INT")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS raw_result_path TEXT")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS hermes_tool_pass BOOLEAN")
        cur.execute("ALTER TABLE benchmark_runs ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'COMPLETED'")
        cur.execute("UPDATE benchmark_runs SET status='COMPLETED' WHERE status IS NULL")
    conn.commit()


@app.get("/api/history/revisions")
def api_history_revisions(request: Request, include_failed: str = ""):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_history_tables(conn)
        with conn.cursor() as cur:
            if include_failed == "true":
                cur.execute("""SELECT revision_id, revision_label, model, artifact, quantization,
                    runtime_version, host, vm, guest_ip, gpu_model, gpu_count, context, parallelism,
                    kv_cache, launch_command, systemd_unit, aliases, status, visible_by_default,
                    preset_id, notes, created_at
                    FROM deployment_revisions ORDER BY revision_id DESC""")
            else:
                cur.execute("""SELECT revision_id, revision_label, model, artifact, quantization,
                    runtime_version, host, vm, guest_ip, gpu_model, gpu_count, context, parallelism,
                    kv_cache, launch_command, systemd_unit, aliases, status, visible_by_default,
                    preset_id, notes, created_at
                    FROM deployment_revisions
                    WHERE visible_by_default = TRUE AND status NOT IN ('FAILED','REJECTED')
                    ORDER BY revision_id DESC""")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, [str(c) if c is not None else None for c in r])) for r in cur.fetchall()]
        return {"revisions": rows}
    except Exception as e:
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/history/revisions")
async def api_history_revision_create(request: Request):
    user, role = current_user(request)
    if not user or role not in CAN_EDIT_DEPLOY:
        return JSONResponse({"error": "role does not permit deployment edits"}, status_code=403)
    body = await request.json()
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_history_tables(conn)
        with conn.cursor() as cur:
            label = body.get("revision_label") or f"{body.get('model','model')}-{int(time.time())}"
            cur.execute("""INSERT INTO deployment_revisions
                (revision_label, model, artifact, artifact_revision, quantization, runtime,
                 runtime_version, host, vm, guest_ip, cpu_cores, ram_gb, gpu_model, gpu_count,
                 context, parallelism, max_num_seqs, kv_cache, launch_command, systemd_unit,
                 aliases, status, visible_by_default, preset_id, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (revision_label) DO UPDATE SET status=EXCLUDED.status, notes=EXCLUDED.notes
                RETURNING revision_id""",
                (label, body.get("model"), body.get("artifact"), body.get("artifact_revision"),
                 body.get("quantization"), body.get("runtime"), body.get("runtime_version"),
                 body.get("host"), body.get("vm"), body.get("guest_ip"), body.get("cpu_cores"),
                 body.get("ram_gb"), body.get("gpu_model"), body.get("gpu_count"),
                 body.get("context"), body.get("parallelism"), body.get("max_num_seqs"),
                 body.get("kv_cache"), body.get("launch_command"), body.get("systemd_unit"),
                 body.get("aliases"), body.get("status", "CANDIDATE"),
                 body.get("visible_by_default", True), body.get("preset_id"), body.get("notes")))
            rid = cur.fetchone()[0]
        conn.commit()
        audit(user, "revision_created", label, "ok", {"revision_id": rid})
        return {"status": "ok", "revision_id": rid, "revision_label": label}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.get("/api/history/benchmarks")
def api_history_benchmarks(request: Request, revision_id: int = None, limit: int = 50):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_history_tables(conn)
        with conn.cursor() as cur:
            if revision_id:
                cur.execute("""SELECT r.*, rev.revision_label, rev.model
                    FROM benchmark_runs r LEFT JOIN deployment_revisions rev
                    ON r.deployment_revision_id = rev.revision_id
                    WHERE r.deployment_revision_id = %s ORDER BY r.run_id DESC LIMIT %s""",
                    (revision_id, limit))
            else:
                cur.execute("""SELECT r.*, rev.revision_label, rev.model
                    FROM benchmark_runs r LEFT JOIN deployment_revisions rev
                    ON r.deployment_revision_id = rev.revision_id
                    ORDER BY r.run_id DESC LIMIT %s""", (limit,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, [str(c) if c is not None else None for c in r])) for r in cur.fetchall()]
        return {"benchmarks": rows}
    except Exception as e:
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/history/benchmarks")
async def api_history_benchmark_add(request: Request):
    user, role = current_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    body = await request.json()
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        _ensure_history_tables(conn)
        f = body
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO benchmark_runs
                (ts, preset, concurrency, requests, ok, failed, duration_s, output_tok_s,
                 ttft_avg_s, ttft_max_s, latency_avg_s, target, notes, deployment_revision_id,
                 prompt_tokens, output_tokens, aggregate_output_tok_s, ram_used_gb, vram_used_gb,
                 tested_context, raw_result_path, hermes_tool_pass, status)
                VALUES (NOW(), %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING run_id""",
                (f.get("preset"), f.get("concurrency"), f.get("requests"), f.get("ok"),
                 f.get("failed"), f.get("duration_s"), f.get("output_tok_s"),
                 f.get("ttft_avg_s"), f.get("ttft_max_s"), f.get("latency_avg_s"),
                 f.get("target"), f.get("notes"), f.get("deployment_revision_id"),
                 f.get("prompt_tokens"), f.get("output_tokens"), f.get("aggregate_output_tok_s"),
                 f.get("ram_used_gb"), f.get("vram_used_gb"), f.get("tested_context"),
                 f.get("raw_result_path"), f.get("hermes_tool_pass"),
                 f.get("status", "COMPLETED")))
            run_id = cur.fetchone()[0]
        conn.commit()
        audit(user, "benchmark_recorded", f.get("target", "?"), "ok", {"run_id": run_id})
        return {"status": "ok", "run_id": run_id}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()


@app.post("/api/history/revisions/{rid}/status")
async def api_history_revision_status(rid: int, request: Request):
    user, role = current_user(request)
    if not user or role not in CAN_EDIT_DEPLOY:
        return JSONResponse({"error": "role does not permit deployment edits"}, status_code=403)
    body = await request.json()
    conn = _pg()
    if not conn:
        return JSONResponse({"error": "postgres unavailable"}, status_code=503)
    try:
        status = body.get("status")
        visible = body.get("visible_by_default")
        with conn.cursor() as cur:
            if status:
                cur.execute("UPDATE deployment_revisions SET status=%s WHERE revision_id=%s", (status, rid))
            if visible is not None:
                cur.execute("UPDATE deployment_revisions SET visible_by_default=%s WHERE revision_id=%s",
                            (visible, rid))
        conn.commit()
        audit(user, "revision_status_updated", rid, "ok", {"status": status})
        return {"status": "ok"}
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": f"{e.__class__.__name__}: {e}"}, status_code=500)
    finally:
        conn.close()



# ---------------------------------------------------------------- dashboard

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    user, role = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    # v0.4: actually render placeholders (v0.3 shipped them un-substituted — role badge,
    # CANEDIT/CANRESTART gating, and this session's OpenRouter panel all depend on it)
    can_provider = role in CAN_WRITE_KEYS
    html = (DASHBOARD_HTML
            .replace("__VER__", APP_VERSION)
            .replace("__USER__", user)
            .replace("__ROLE__", role or "")
            .replace("__OR_DISABLED__", "" if can_provider else "disabled")
            .replace("__OR_TOGGLE__", "Enable fallback" if can_provider else "Enable fallback (Admin only)"))
    return HTMLResponse(html)


@app.get("/")
def root():
    return RedirectResponse("/admin", status_code=302)


DASHBOARD_HTML = r"""<!doctype html><html><head><title>LLM Manager</title><style>
body{background:#0d1117;color:#e6edf3;font-family:system-ui;margin:0;padding:1.2rem}
h1{font-size:1.2rem} h2{font-size:1rem;color:#58a6ff;margin:1.2rem 0 .5rem}
.top{display:flex;justify-content:space-between;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:.8rem}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:.8rem;font-size:.85rem}
.ok{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}
button{background:#238636;border:0;color:#fff;border-radius:6px;padding:.45rem .8rem;cursor:pointer;font-weight:600}
button.gray{background:#30363d} button.red{background:#b62324}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th,td{border-bottom:1px solid #21262d;padding:.4rem .5rem;text-align:left}
input,textarea,select{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:.45rem;font-family:inherit}
textarea{width:100%;box-sizing:border-box;font-family:monospace;font-size:.75rem}
.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin:.4rem 0}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;justify-content:center;align-items:center}
.mcard{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:1.4rem;width:560px}
.key{font-family:monospace;background:#0d1117;padding:.6rem;border-radius:6px;word-break:break-all;user-select:all}
pre{background:#0d1117;padding:.6rem;border-radius:6px;overflow:auto;max-height:300px;font-size:.72rem}
.badge{background:#1f6feb;color:#fff;border-radius:10px;padding:.1rem .5rem;font-size:.7rem}
</style></head><body>
<div class="top">
 <h1>🧠 MARION-IA-USA LLM Manager <span class="badge">__VER__</span></h1>
 <div>__USER__ <span class="badge">__ROLE__</span> <a href="/logout" style="color:#58a6ff">logout</a></div>
</div>

<h2>Inference hosts</h2><div class="grid" id="hosts"></div>

<h2>API keys <span style="font-size:.75rem;color:#8b949e">(LiteLLM virtual keys · access: all currently hosted models · no default model — §v0.11 E1)</span></h2>
<div class="card">
 <div class="row">
  <input id="k-alias" placeholder="alias (e.g. hermes-vm910-pilot)" style="width:260px">
  <input id="k-budget" placeholder="monthly $ cap (optional)" style="width:170px">
  <input id="k-days" placeholder="days (30)" style="width:90px" value="30">
  <button onclick="issueKey()">Issue key</button>
  <button class="gray" onclick="loadKeys()">Refresh</button>
 </div>
 <table id="keys-t"><thead><tr><th>alias</th><th>key</th><th>spend/cap</th><th>state</th><th>actions</th></tr></thead><tbody></tbody></table>
 <div id="keys-msg" style="color:#8b949e;font-size:.75rem"></div>
</div>

<h2>Deployments — vLLM model &amp; lifecycle control <span style="font-size:.75rem;color:#8b949e">(qualification envelopes applied · admission via rpm caps)</span></h2>
<div class="card" id="deploys"><div style="color:#8b949e">Loading…</div></div>

<h2>📜 Deployment history <span style="font-size:.75rem;color:#8b949e">(revisions + benchmark runs · v5 2026-09-23)</span> <label style="font-size:.8rem"><input type="checkbox" id="hist-failed" onchange="loadHistory()"> show failed/rejected</label></h2>
<div class="card" id="hist-card"><div style="color:#8b949e">Loading…</div></div>

<h2>⚡ Power &amp; Cost <span style="font-size:.75rem;color:#8b949e">(GPU NVML telemetry · 1-min rollups · seasonal rate)</span></h2>
<div class="card" id="power-card">
 <div class="row" id="power-rate" style="color:#8b949e;font-size:.78rem"></div>
 <table id="power-t"><thead><tr><th>host</th><th>GPUs now (W)</th><th>util</th><th>avg W (60m)</th><th>peak W (60m)</th></tr></thead><tbody></tbody></table>
 <div id="power-msg" style="color:#8b949e;font-size:.72rem;margin-top:.4rem"></div>
</div>
<div class="card" id="cost-card" style="margin-top:.6rem">
 <div class="row" id="cost-row" style="font-size:.85rem">Loading cost…</div>
 <div id="cost-note" style="color:#8b949e;font-size:.72rem;margin-top:.3rem"></div>
</div>

<h2>💰 Spend &amp; Efficiency <span style="font-size:.75rem;color:#8b949e">(OpenRouter-style analytics · GPU-attributed energy · seasonal rate)</span></h2>
<div class="card" id="spend-card">
 <div class="row" style="margin-bottom:.5rem">
  <label style="font-size:.75rem;color:#8b949e">range:</label>
  <select id="sp-range" onchange="spRangeChanged()" style="background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:2px 6px">
   <option value="24">24h</option><option value="168" selected>7d</option>
   <option value="720">30d</option><option value="custom">custom</option>
  </select>
  <input type="date" id="sp-from" style="background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:2px 6px;display:none">
  <input type="date" id="sp-to" style="background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:2px 6px;display:none">
  <button class="gray" onclick="loadSpend()" style="padding:2px 10px">apply</button>
  <span id="sp-msg" style="color:#8b949e;font-size:.72rem"></span>
  <span style="flex:1"></span>
  <span id="sp-usage" style="font-size:.8rem">Loading…</span>
 </div>
 <div id="spend-row" style="font-size:.85rem"></div>
 <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-top:.6rem">
  <div style="flex:2;min-width:420px">
   <div style="font-size:.78rem;color:#8b949e;margin-bottom:.3rem">models — provider spend vs direct compute are separate metrics (§H)</div>
   <table id="sp-models" style="width:100%;font-size:.78rem"><thead><tr>
    <th style="text-align:left">Model</th><th>Provider $</th><th>Compute basis</th><th>Totals</th><th>Reqs</th><th>tokens</th>
   </tr></thead><tbody></tbody></table>
  </div>
  <div style="flex:1;min-width:260px">
   <div style="font-size:.78rem;color:#8b949e;margin-bottom:.3rem">by API key</div>
   <table id="sp-keys" style="width:100%;font-size:.78rem"><thead><tr>
    <th style="text-align:left">Key</th><th>$</th><th>tok</th><th>reqs</th>
   </tr></thead><tbody></tbody></table>
  </div>
  <div style="flex:1;min-width:240px">
   <div style="font-size:.78rem;color:#8b949e;margin-bottom:.3rem">by day</div>
   <div id="sp-days" style="font-size:.75rem"></div>
  </div>
 </div>
 <div id="spend-note" style="color:#8b949e;font-size:.72rem;margin-top:.3rem"></div>
</div>

<h2>🔌 Host Power Policy <span style="font-size:.75rem;color:#8b949e">(desired state vs current · recovery engine · plan §B)</span></h2>
<div class="card" id="power-policy-card">
 <div style="font-size:.75rem;color:#8b949e;margin-bottom:.4rem">
  <b>Online / managed</b>: auto-recovered when unexpectedly stopped (bounded: 2 VM starts, 2 service restarts).
  <b>Intentionally offline</b>: energy saving — never auto-recovered, never shown as outage, removed from routing.
 </div>
 <table id="policy-t" style="width:100%;font-size:.78rem"><thead><tr>
  <th style="text-align:left">Host</th><th>Desired</th><th>Mode</th><th>VM now</th><th>Recent events</th><th>Actions</th>
 </tr></thead><tbody></tbody></table>
 <div id="policy-msg" style="color:#8b949e;font-size:.72rem;margin-top:.3rem"></div>
</div>

<h2>🕸️ Distributed (RDMA ring) <span style="font-size:.75rem;color:#8b949e">(100 Gb ring 111↔143↔144 · multi-node vLLM · plan §19)</span></h2>
<div class="card" id="rdma-card">
 <div style="font-size:.78rem;color:#8b949e;margin-bottom:.4rem">Ring nodes — a multi-node deployment cannot enter routing until every participant is RDMA-qualified (§19.8)</div>
 <table id="rdma-t" style="width:100%;font-size:.78rem"><thead><tr>
  <th style="text-align:left">Host</th><th>IP</th><th>VM</th><th>NIC</th><th>link</th><th>qualified</th><th>RDMA</th><th>notes</th>
 </tr></thead><tbody></tbody></table>
 <div class="row" style="margin-top:.6rem">
  <input id="rd-name" placeholder="deployment name" style="width:150px" __OR_DISABLED__>
  <input id="rd-model" placeholder="model repo (e.g. Qwen/Qwen3.6-35B-AWQ)" style="width:260px" __OR_DISABLED__>
  <input id="rd-tp" placeholder="TP" style="width:60px" value="2" __OR_DISABLED__>
  <input id="rd-pp" placeholder="pp" style="width:60px" value="1" __OR_DISABLED__>
  <button class="gray" onclick="rdmaCreate()" __OR_DISABLED__>Generate multi-node config</button>
 </div>
 <div class="row" style="font-size:.72rem;color:#8b949e">participants:</div>
 <div class="row" id="rd-hosts"></div>
 <div id="rdma-msg" style="color:#8b949e;font-size:.75rem;margin-top:.4rem"></div>
 <pre id="rdma-config" style="display:none;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:.6rem;font-size:.72rem;overflow:auto;max-height:300px"></pre>
</div>

<h2>☁️ OpenRouter fallback <span style="font-size:.75rem;color:#8b949e">(provider master config · hard caps gate — plan §9.3)</span></h2>
<div class="card" id="or-card">
 <div id="or-state" style="font-size:.82rem;margin-bottom:.5rem"></div>
 <div class="row">
  <input id="or-key" placeholder="OpenRouter key (sk-or-…)" type="password" style="width:230px" __OR_DISABLED__>
  <input id="or-daily" placeholder="daily cap $" style="width:110px" __OR_DISABLED__>
  <input id="or-monthly" placeholder="monthly cap $" style="width:120px" __OR_DISABLED__>
  <input id="or-warn" placeholder="warn threshold $" style="width:130px" __OR_DISABLED__>
 </div>
 <div class="row">
  <button class="gray" onclick="orSave()">Save config</button>
  <button class="red" onclick="orToggle()">__OR_TOGGLE__</button>
  <span id="or-msg" style="color:#8b949e;font-size:.75rem"></span>
 </div>
 <div style="color:#8b949e;font-size:.72rem;margin-top:.4rem">
  Fallback stays DISABLED until key + daily cap + monthly cap + warning threshold all exist.
  Routing integration (local queue timeout → OpenRouter) is a later phase; this page only governs config and policy state.
 </div>
</div>

<div id="modal"></div>
<script>
const CANEDIT = ["Admin","Security-Admin"].includes("__ROLE__");
const CANRESTART = ["Operator","Admin","Security-Admin"].includes("__ROLE__");

async function j(url, opts){const r=await fetch(url,opts);if(r.status===401){location='/login';throw 0}return r}

async function loadStatus(){
  const d=await (await j('/api/status')).json();
  document.getElementById('hosts').innerHTML=d.hosts.map(h=>
   `<div class="card"><b>${h.name}</b><br>
    <span class="${h.status==='healthy'?'ok':'bad'}">● ${h.status}</span> ${h.latency_ms??''}ms<br>
    model: ${h.model??'—'}<br>ctx: ${h.max_model_len??'—'}<br>
    ${h.error?`<span class="bad">${h.error}</span>`:''}</div>`).join('')+
   `<div class="card"><b>PostgreSQL</b><br><span class="${d.postgres.status==='healthy'?'ok':'bad'}">● ${d.postgres.status}</span> ${d.postgres.latency_ms??''}ms<br>${d.postgres.version??d.postgres.error??''}</div>`;
}
async function loadKeys(){
  try{
    const d=await (await j('/api/keys')).json();
    if(d.error){document.getElementById('keys-msg').textContent=d.error;return}
    document.querySelector('#keys-t tbody').innerHTML=(d.keys||[]).map(k=>
      `<tr><td>${k.alias??''}</td><td style="font-family:monospace">${k.token??''}</td>
       <td>$${k.spend??0} / ${k.max_budget??'∞'}</td>
       <td class="${k.blocked?'bad':'ok'}">${k.blocked?'blocked':'active'}</td>
       <td>${CANEDIT?`<button class="gray" onclick="blockKey('${k.alias}',${k.blocked})">${k.blocked?'unblock':'block'}</button>
       <button class="red" onclick="delKey('${k.alias}')">revoke</button>`:''}</td></tr>`).join('')
      || '<tr><td colspan=5 style="color:#8b949e">no keys yet</td></tr>';
  }catch(e){}
}
async function issueKey(){
  const alias=document.getElementById('k-alias').value.trim();
  const budget=document.getElementById('k-budget').value;
  const days=document.getElementById('k-days').value||30;
  if(!alias)return msg('alias required');
  const r=await j('/api/keys/issue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alias,budget_usd:budget,days})});
  const d=await r.json();
  if(d.key){showKeyModal(d.key,alias)}else{msg(d.error||'issue failed')}
}
function msg(t){document.getElementById('keys-msg').textContent=t}
function showKeyModal(key,alias){
  document.getElementById('modal').innerHTML=`<div class="modal"><div class="mcard">
   <h2 style="margin-top:0">🔑 Key issued: ${alias}</h2>
   <p style="font-size:.8rem;color:#d29922">Copy now — shown only once.</p>
   <div class="key" id="thekey">${key}</div>
   <div class="row" style="margin-top:.8rem">
    <button onclick="navigator.clipboard.writeText('${key}');msg('copied')">Copy</button>
    <button class="gray" onclick="document.getElementById('modal').innerHTML=''">Close</button>
   </div></div></div>`;
  loadKeys();
}
async function blockKey(alias,blocked){
  const r=await j('/api/keys/block',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:alias,unblock:blocked})});
  const d=await r.json(); if(d.error)msg(d.error);
  loadKeys();
}
async function delKey(alias){
  if(!confirm('Revoke key "'+alias+'"? Clients using it fail immediately.'))return;
  const r=await j('/api/keys/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:alias})});
  const d=await r.json(); if(d.error)msg(d.error);
  loadKeys();
}
async function loadDeploys(){
  const el=document.getElementById('deploys');
  const d=await (await j('/api/deployments')).json();
  el.innerHTML=d.deployments.map(x=>`
   <div style="border-bottom:1px solid #21262d;padding:.6rem 0">
    <div class="row"><b>${x.name}</b> <span class="badge">${x.ip}</span>
     <span style="color:#8b949e;font-size:.75rem">${(x.status||'').slice(0,120)}</span></div>
    ${CANEDIT?`<div class="row"><button class="gray" onclick="showUnit('${x.ip}')">Edit unit / model</button>`:''}
     ${CANRESTART?`<button onclick="restartDep('${x.ip}')">Restart vLLM</button>`:''}
     <button class="gray" onclick="showLogs('${x.ip}')">Logs</button></div>
    <div id="out-${x.ip}" style="font-size:.72rem;color:#8b949e"></div>
   </div>`).join('');
}
async function showUnit(ip){
  const d=await (await j('/api/deployments')).json();
  const dep=d.deployments.find(x=>x.ip===ip);
  document.getElementById('modal').innerHTML=`<div class="modal"><div class="mcard" style="width:860px;max-height:88vh;overflow:auto">
   <h2 style="margin-top:0">Edit unit / model — ${ip}</h2>
   <div id="preset-panel" style="border:1px solid #30363d;border-radius:8px;padding:.6rem;margin-bottom:.8rem;background:#0d1117">
    <div style="font-weight:600;color:#58a6ff;font-size:.85rem;margin-bottom:.4rem">Hosted command history / presets</div>
    <table id="preset-t" style="font-size:.75rem"><thead><tr><th></th><th>name</th><th>engine</th><th>last used</th><th>uses</th><th>actions</th></tr></thead><tbody></tbody></table>
    <div class="row" style="margin-top:.4rem">
     <input id="preset-name" placeholder="friendly name for current command" style="width:240px">
     <button class="gray" onclick="savePreset('${ip}')">Save current as preset</button>
     <span style="color:#8b949e;font-size:.7rem">identical commands dedup automatically</span>
    </div>
    <div id="preset-msg" style="color:#8b949e;font-size:.7rem;margin-top:.3rem"></div>
   </div>
   <textarea id="unit-ta" rows="14">${(dep.unit||'').replace(/</g,'&lt;')}</textarea>
   <div class="row"><button onclick="saveUnit('${ip}')">Save unit</button>
   <span style="color:#d29922;font-size:.75rem">Save does NOT restart. Restart after saving to apply.</span>
   <button class="gray" onclick="document.getElementById('modal').innerHTML=''">Close</button></div>
   <div id="unit-msg" style="font-size:.75rem"></div></div></div>`;
  loadPresets(ip);
}
let dragPid=null;
async function loadPresets(ip){
  try{
    const d=await (await j(`/api/hosts/${ip}/presets`)).json();
    if(d.error){document.getElementById('preset-msg').textContent=d.error;return}
    document.querySelector('#preset-t tbody').innerHTML=(d.presets||[]).map(p=>`
     <tr draggable="true" ondragstart="dragPid=${p.id}" ondragover="event.preventDefault()" ondrop="dropPreset('${ip}')">
      <td style="cursor:grab;color:#8b949e" title="drag to reorder">&#x2630;</td>
      <td style="text-align:left"><b>${p.friendly_name}</b></td><td>${d.engine}</td>
      <td style="color:#8b949e">${p.last_used_at?p.last_used_at.slice(0,16):'-'}</td><td>${p.use_count||0}</td>
      <td style="text-align:left">
       <button class="gray" onclick="usePreset('${ip}',${p.id})">Use</button>
       ${CANEDIT?`<button class="gray" onclick="renamePreset('${ip}',${p.id},'${p.friendly_name.replace(/'/g,"\\'")}')">Rename</button>
       <button class="red" onclick="delPreset('${ip}',${p.id})">Delete</button>`:''}
      </td></tr>`).join('')||'<tr><td colspan=6 style="color:#8b949e">no presets yet</td></tr>';
  }catch(e){}
}
async function usePreset(ip,pid){
  const r=await j(`/api/hosts/${ip}/presets/${pid}/use`,{method:'POST'});
  const d=await r.json();
  if(d.error){document.getElementById('preset-msg').innerHTML='<span class="bad">'+d.error+'</span>';return}
  document.getElementById('unit-ta').value=d.content;
  document.getElementById('unit-msg').innerHTML='<span class="ok">preset loaded into editor. Save unit, then Restart separately (save alone never restarts)</span>';
  loadPresets(ip);
}
async function savePreset(ip){
  const content=document.getElementById('unit-ta').value;
  const friendly=document.getElementById('preset-name').value;
  const r=await j(`/api/hosts/${ip}/presets`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,friendly_name:friendly})});
  const d=await r.json();
  document.getElementById('preset-msg').innerHTML=d.error?('<span class="bad">'+d.error+'</span>'):(d.status==='reused'?'<span class="ok">existing identical preset reused (moved to top)</span>':'<span class="ok">saved as new preset</span>');
  document.getElementById('preset-name').value='';
  loadPresets(ip);
}
async function renamePreset(ip,pid,cur){
  const nn=prompt('New name for preset:',cur);
  if(!nn||nn===cur)return;
  await j(`/api/hosts/${ip}/presets/${pid}/rename`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({friendly_name:nn})});
  loadPresets(ip);
}
async function delPreset(ip,pid){
  if(!confirm('Delete this preset? History entry only - the currently running service is NOT touched.'))return;
  await j(`/api/hosts/${ip}/presets/${pid}/delete`,{method:'POST'});
  loadPresets(ip);
}
async function dropPreset(ip){
  const rows=[...document.querySelectorAll('#preset-t tbody tr[draggable]')];
  const order=rows.map(r=>{const m=r.innerHTML.match(/usePreset\('${ip}',(\d+)\)/);return m?+m[1]:null}).filter(Boolean);
  await j(`/api/hosts/${ip}/presets/reorder`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order})});
  loadPresets(ip);
}
async function saveUnit(ip){
  const unit=document.getElementById('unit-ta').value;
  const r=await j(`/api/deployments/${ip}/unit`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({unit})});
  const d=await r.json();
  document.getElementById('unit-msg').innerHTML=(d.status==='ok'?'<span class="ok">saved: ':'<span class="bad">error: ')+(d.output||d.error||'').slice(0,300)+'</span>';
  loadDeploys();
}
async function restartDep(ip){
  if(!confirm('Restart model service on '+ip+' now?\nActive requests may be interrupted. Reload takes ~2-5 min.'))return;
  const el=document.getElementById('out-'+ip);el.textContent='restarting…';
  const r=await j(`/api/deployments/${ip}/restart`,{method:'POST'});
  const d=await r.json();el.textContent=d.output||d.status;loadStatus();
}
async function showLogs(ip){
  const r=await j(`/api/deployments/${ip}/logs`);
  const d=await r.json();
  document.getElementById('modal').innerHTML=`<div class="modal"><div class="mcard" style="width:820px">
   <h2 style="margin-top:0">vLLM logs — ${ip}</h2><pre>${(d.logs||'').replace(/</g,'&lt;').slice(-5000)}</pre>
   <button class="gray" onclick="document.getElementById('modal').innerHTML=''">Close</button></div></div>`;
}
async function loadPower(){
  try{
    const p=await (await j('/api/power?minutes=60')).json();
    const rateEl=document.getElementById('power-rate');
    if(p.error||!p.rate){rateEl.textContent='power telemetry unavailable: '+(p.error||'no data');}
    else{
      rateEl.innerHTML=`effective rate: <b>${p.rate.display_cents}¢/kWh</b> (${p.rate.season}) · source: ${p.rate.source}`;
      document.querySelector('#power-t tbody').innerHTML=(p.hosts||[]).map(h=>{
        const util=(h.gpus||[]).map(g=>g.util_pct??'?').join('/');
        const avg=h.series&&h.series.length?h.series[h.series.length-1].avg:'—';
        const peak=h.series&&h.series.length?Math.max(...h.series.map(s=>s.peak||0)):'—';
        return `<tr><td>${h.name}</td><td><b>${h.current_gpu_watts??'—'}</b></td>
         <td style="font-size:.75rem;color:#8b949e">${util}</td><td>${avg??'—'}</td><td>${peak||'—'}</td></tr>`;
      }).join('')||'<tr><td colspan=5 style="color:#8b949e">no host data</td></tr>';
      document.getElementById('power-msg').textContent=p.collector?
        (p.collector.healthy?`collector healthy · ${p.collector.samples_5m} samples in 5m · last ${p.collector.last_ts?.slice(11,19)}Z`
        :'collector NOT writing samples — check llm-manager-collector service')
        :'collector not reporting';
    }
  }catch(e){}
}
async function loadCost(){
  try{
    const c=await (await j('/api/cost')).json();
    const row=document.getElementById('cost-row');
    if(c.error){row.textContent='cost unavailable: '+c.error;return}
    const w=c.windows||{};
    let html=Object.entries(w).map(([k,v])=>
      `<span style="margin-right:1.2rem"><b>${k}</b>: ${v.gpu_kwh} kWh ≈ <b>$${v.gpu_cost_usd}</b></span>`).join('')+
      ` <span style="color:#8b949e;font-size:.75rem">rate ${c.rate.effective_per_kwh}/kWh</span>`;
    if(c.facility){
      const r=c.facility;
      const f=(role,label)=>r[role]?`<span style="margin-right:1.2rem">facility ${label}: <b>${r[role].avg_watts} W</b> · ${r[role].kwh??'—'} kWh (24h)</span>`:'';
      html+='<br>'+ (f('compute','compute')+f('cooling','cooling')+f('other','other') ||
        'facility: data present, no role mapping yet');
    }
    row.innerHTML=html;
    document.getElementById('cost-note').textContent=c.note||'';
  }catch(e){}
}
async function loadOpenRouter(){
  try{
    const s=await (await j('/api/openrouter')).json();
    const el=document.getElementById('or-state');
    const gate=s.fallback_enabled?
      '<span class="ok">● fallback ENABLED</span>':
      '<span class="warn">○ fallback disabled</span>';
    const errs=(s.gate_errors||[]).map(e=>`<span class="bad">${e}</span>`).join(' · ')||'caps checklist satisfied';
    el.innerHTML=`${gate}<br><span style="font-size:.75rem;color:#8b949e">${errs}</span>`+
      (s.key_masked?`<br><span style="font-size:.75rem">key: ${s.key_masked}</span>`:'');
    const t=document.getElementById('or-toggle-text');
    if(t)t.textContent=s.fallback_enabled?'Disable fallback':'Enable fallback';
  }catch(e){}
}
async function orSave(){
  const body={};
  const k=document.getElementById('or-key').value.trim(); if(k)body.api_key=k;
  for(const [id,f] of [['or-daily','daily_cap_usd'],['or-monthly','monthly_cap_usd'],['or-warn','warning_threshold_usd']]){
    const v=document.getElementById(id).value; if(v!=='')body[f]=v;
  }
  if(!Object.keys(body).length){document.getElementById('or-msg').textContent='nothing to save';return}
  const r=await j('/api/openrouter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  document.getElementById('or-msg').textContent=d.error?('error: '+d.error+(d.gate_errors?' — '+d.gate_errors.join('; '):'')):'saved';
  if(!d.error)document.getElementById('or-key').value='';
  loadOpenRouter();
}
async function orToggle(){
  const r=await j('/api/openrouter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enable_fallback:true})});
  const d=await r.json();
  document.getElementById('or-msg').textContent=d.error?('BLOCKED: '+d.error+(d.gate_errors?' — '+d.gate_errors.join('; '):'')):'fallback state updated';
  loadOpenRouter();
}
function spRangeChanged(){
  const custom=document.getElementById('sp-range').value==='custom';
  document.getElementById('sp-from').style.display=custom?'':'none';
  document.getElementById('sp-to').style.display=custom?'':'none';
  if(!custom)loadSpend();
}
async function loadSpend(){
  try{
    const sel=document.getElementById('sp-range').value;
    let url='/api/spend/analytics?';
    if(sel==='custom'){
      const f=document.getElementById('sp-from').value;
      if(!f){document.getElementById('sp-msg').textContent='pick a start date';return}
      url+='from_='+encodeURIComponent(f)+'&to='+encodeURIComponent(document.getElementById('sp-to').value||'');
    }else{url+='hours='+sel}
    const s=await (await j(url)).json();
    if(s.error){document.getElementById('sp-msg').textContent=s.error;return}
    document.getElementById('sp-msg').textContent=s.range;
    const u=s.usage;
    document.getElementById('sp-usage').innerHTML=
      `<span style="margin-right:1rem">Total reqs <b>${(u.all_time.requests||0).toLocaleString()}</b></span>`+
      `<span style="margin-right:1rem">Today <b>$${(u.today.spend_usd??0).toFixed(2)}</b> · ${(u.today.prompt_tokens+u.today.completion_tokens).toLocaleString()} tok</span>`+
      `<span style="margin-right:1rem">This Week <b>$${(u.this_week.spend_usd??0).toFixed(2)}</b> · ${(u.this_week.prompt_tokens+u.this_week.completion_tokens).toLocaleString()} tok</span>`+
      `<span>This Month <b>$${(u.this_month.spend_usd??0).toFixed(2)}</b> · ${(u.this_month.prompt_tokens+u.this_month.completion_tokens).toLocaleString()} tok</span>`;
    document.querySelector('#sp-models tbody').innerHTML=(s.models||[]).map(m=>
      `<tr><td style="text-align:left">${m.model}</td>
       <td><b>$${(m.provider_spend_usd??0).toFixed(4)}</b> <span style="color:#8b949e;font-size:.7rem">${m.provider_spend_basis??''}</span></td>
       <td style="color:#8b949e">${m.direct_compute??'—'}</td>
       <td><b>$${(m.total_spend_usd??0).toFixed(4)}</b></td>
       <td>${m.requests}</td><td style="color:#8b949e">${(m.total_tokens||0).toLocaleString()}</td></tr>`).join('')
      || '<tr><td colspan=6 style="color:#8b949e">no model traffic in range</td></tr>';
    document.querySelector('#sp-keys tbody').innerHTML=(s.keys||[]).map(k=>
      `<tr><td style="text-align:left">${k.key}</td><td><b>$${(k.spend_usd??0).toFixed(4)}</b></td>
       <td style="color:#8b949e">${(k.total_tokens||0).toLocaleString()}</td><td>${k.requests}</td></tr>`).join('')
      || '<tr><td colspan=4 style="color:#8b949e">no key traffic in range</td></tr>';
    const mx=Math.max(...(s.days||[]).map(d=>d.total_tokens||0),1);
    document.getElementById('sp-days').innerHTML=(s.days||[]).map(d=>
      `<div style="margin-bottom:2px"><span style="display:inline-block;width:150px;color:#8b949e">${d.day}</span>`+
      `<span style="display:inline-block;height:10px;background:#3fb950;border-radius:2px;width:${Math.max(2,(d.total_tokens/mx)*150)}px"></span>`+
      ` <b>$${(d.spend_usd??0).toFixed(2)}</b> · ${(d.total_tokens||0).toLocaleString()} tok</div>`).join('')
      || '<span style="color:#8b949e">no daily data</span>';
    document.getElementById('spend-note').textContent=
      (s.note||'') + (s.window_energy?.telemetry_coverage_pct!=null ?
      ` · telemetry coverage ${s.window_energy.telemetry_coverage_pct}% (window_energy.basis: ${s.window_energy.basis})` : '');
  }catch(e){}
}
async function loadRdma(){
  try{
    const d=await (await j('/api/rdma/ring')).json();
    if(d.error){return}
    document.querySelector('#rdma-t tbody').innerHTML=(d.nodes||[]).map(n=>
      `<tr><td style="text-align:left"><label style="font-weight:normal"><input type="checkbox" class="rdma-host" value="${n.host}"> ${n.host}</label></td>`+
      `<td style="font-family:monospace">${n.ip}</td><td>${n.vmid??'—'}</td><td>${n.nic_model||'—'}</td>`+
      `<td>${n.link_state||'?'}</td><td class="${n.qualified?'ok':'warn'}">${n.qualified?'✓ qualified':'○ not yet'}</td>`+
      `<td style="color:#8b949e;font-size:.72rem">${(n.notes||'').slice(0,60)}</td>`+
      `<td>${CANEDIT?`<button class="${n.rdma_enabled?'red':'gray'}" onclick="rdmaToggle('${n.host}',${!n.rdma_enabled})">${n.rdma_enabled?'Disable RDMA':'Enable RDMA'}</button>`:(n.rdma_enabled?'on':'off')}</td></tr>`).join('');
  }catch(e){}
}
async function rdmaToggle(host,want){
  if(!confirm((want?'Enable':'Disable')+' RDMA hosting for '+host+'? Does not affect the physical ring.'))return;
  const r=await j(`/api/rdma/ring/${host}/toggle`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rdma_enabled:want})});
  const d=await r.json();
  if(d.error)document.getElementById('rdma-msg').innerHTML='<span class="bad">'+d.error+'</span>';
  loadRdma();
}
async function rdmaCreate(){
  const hosts=[...document.querySelectorAll('.rdma-host:checked')].map(c=>c.value);
  if(hosts.length<2){document.getElementById('rdma-msg').textContent='pick at least 2 participating hosts';return}
  const body={name:document.getElementById('rd-name').value.trim(),
              model:document.getElementById('rd-model').value.trim(),
              participating_hosts:hosts,
              tp:parseInt(document.getElementById('rd-tp').value||'2'),
              pp:parseInt(document.getElementById('rd-pp').value||'1')};
  const r=await j('/api/rdma/deployments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  const el=document.getElementById('rdma-msg');
  if(d.error){el.innerHTML='<span class="bad">'+d.error+'</span>';return}
  el.innerHTML=(d.gate_errors&&d.gate_errors.length?
      '<span class="bad">§19.8 gate: '+d.gate_errors.join(' · ')+'</span> — deployment saved as <b>planned</b>, NOT routable'
      :'<span class="ok">saved (id '+d.id+')</span>');
  const pre=document.getElementById('rdma-config');
  pre.style.display='';pre.textContent=d.config||'';
}
async function loadPolicy(){
  try{
    const d=await (await j('/api/hosts/state')).json();
    if(d.error){document.getElementById('policy-msg').textContent=d.error;return}
    document.querySelector('#policy-t tbody').innerHTML=(d.hosts||[]).map(h=>{
      const desired=h.desired_power_state==='RUNNING'?'ok':'warn';
      const cur=h.vm_status==='running'?'ok':(h.vm_status?'bad':'warn');
      const ev=(h.events||[]).slice(0,2).map(e=>`${e.type}@${e.ts.slice(11,16)}Z`).join(', ')||'—';
      const isAdm=CANEDIT;
      return `<tr><td style="text-align:left"><b>${h.name}</b></td>
       <td class="${desired}">${h.desired_power_state}</td>
       <td>${h.management_mode}</td>
       <td class="${cur}">${h.vm_status??'—'}</td>
       <td style="color:#8b949e;font-size:.7rem">${ev}</td>
       <td>${isAdm?`<button class="gray" onclick="setDesired('${h.ip}','RUNNING')">Online</button>
       <button class="gray" onclick="setDesired('${h.ip}','STOPPED_INTENTIONAL')">Off (energy)</button>`:''}</td></tr>`;
    }).join('');
  }catch(e){}
}
async function setDesired(ip,state){
  const label=state==='RUNNING'?'ONLINE (auto-recovery enabled)':'INTENTIONALLY OFFLINE (never auto-recovered, removed from routing)';
  if(!confirm(`Set ${ip} desired state to ${label}?`))return;
  const r=await j(`/api/hosts/${ip}/desired-state`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({desired_power_state:state})});
  const d=await r.json();
  document.getElementById('policy-msg').textContent=d.error?('error: '+d.error):('desired state updated: '+ip+' → '+state);
  loadPolicy();
}
loadStatus();loadKeys();loadDeploys();loadPower();loadCost();loadOpenRouter();loadSpend();loadRdma();loadPolicy();
setInterval(loadStatus,15000);
setInterval(loadPower,60000);
setInterval(loadPolicy,60000);

// -------- history (v5) --------
async function loadHistory(){
  const failed = document.getElementById('hist-failed') && document.getElementById('hist-failed').checked ? '?include_failed=true' : '';
  const h = await fetch('/api/history/revisions'+failed).then(r=>r.json()).catch(()=>({}));
  const b = await fetch('/api/history/benchmarks?limit=25').then(r=>r.json()).catch(()=>({}));
  const revs = (h.revisions||[]);
  const bms = (b.benchmarks||[]);
  let html = '<table style="width:100%;font-size:.82rem"><tr style="text-align:left;color:#8b949e"><th>revision</th><th>model</th><th>host/VM</th><th>ctx</th><th>parallelism</th><th>status</th><th>preset</th></tr>';
  for (const r of revs.slice(0,20)) {
    html += `<tr><td>${r.revision_label}</td><td>${r.model||''}</td><td>${r.host||''} / ${r.vm||''}</td><td>${r.context||''}</td><td>${r.parallelism||''}</td><td>${r.status||''}</td><td>${r.preset_id||''}</td></tr>`;
  }
  html += '</table>';
  html += '<h3 style="margin:.6rem 0 .2rem">Benchmark runs (latest 25)</h3>';
  html += '<table style="width:100%;font-size:.82rem"><tr style="text-align:left;color:#8b949e"><th>ts</th><th>target</th><th>conc</th><th>ok/fail</th><th>tok/s</th><th>TTFT avg/max</th><th>ctx</th><th>revision</th><th>notes</th></tr>';
  for (const x of bms.slice(0,25)) {
    html += `<tr><td>${(x.ts||'').slice(0,16).replace('T',' ')}</td><td>${x.target||''}</td><td>${x.concurrency||''}</td><td>${x.ok||0}/${x.failed||0}</td><td>${x.aggregate_output_tok_s||x.output_tok_s||''}</td><td>${x.ttft_avg_s||''}/${x.ttft_max_s||''}</td><td>${x.tested_context||''}</td><td>${x.revision_label||''}</td><td>${(x.notes||'').slice(0,60)}</td></tr>`;
  }
  html += '</table>';
  document.getElementById('hist-card').innerHTML = html;
}
loadHistory();
</script></body></html>"""
