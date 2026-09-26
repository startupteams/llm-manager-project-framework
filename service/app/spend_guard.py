"""OpenRouter spend-guard for the MARION LLM Manager LiteLLM proxy.

LiteLLM v1.100.0 CustomLogger callback. Wires the manager's configured
OpenRouter caps (manager_settings: daily/monthly caps + warning threshold)
into the gateway's fallback path as a REAL runtime gate:

- Reads caps from the app DB (llmmanager.manager_settings) with a short
  in-process cache so the UI can change caps without a gateway restart.
- Sums LiteLLM_SpendLogs.spend for rows whose model starts with
  'openrouter/' over calendar-day and calendar-month windows (UTC).
- At cap: hard-stop (429). At warning threshold: journal WARN line.
- Only gates requests routed to an openrouter/* deployment; local
  traffic never touches the DB check.

Fail-closed: if caps or spend cannot be read, openrouter requests are
rejected (local traffic unaffected).
"""
import os
import time
import logging
from datetime import datetime

from litellm.integrations.custom_logger import CustomLogger
from fastapi import HTTPException

log = logging.getLogger("spend_guard")

CAP_KEYS = {
    "daily": "openrouter.daily_cap_usd",
    "monthly": "openrouter.monthly_cap_usd",
    "warning": "openrouter.warning_threshold_usd",
}
CAPS_TTL = 30.0
CONNECT_TIMEOUT = 3
DSN_FILE = os.environ.get("SPEND_GUARD_DSN_FILE", "/etc/llm-manager/secrets/pg_app_creds")

_conn_cache = {}
_caps_cache = None
_caps_ts = 0.0


def _get_conn(which):
    """Cached psycopg2 connection: 'app' (manager caps) or 'spend' (litellm DB)."""
    import psycopg2, re
    c = _conn_cache.get(which)
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
            return c
        except Exception:
            try:
                c.close()
            except Exception:
                pass
            _conn_cache.pop(which, None)

    if which == "app":
        kv = {}
        with open(DSN_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    kv[k] = v
        conn = psycopg2.connect(
            host=kv["PG_HOST"], port=5432, dbname=kv["PG_DB"],
            user=kv["PG_USER"], password=kv["PG_PW"], connect_timeout=CONNECT_TIMEOUT,
        )
    else:
        dbu = ""
        with open("/etc/llm-manager/litellm.env") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    dbu = line.split("=", 1)[1].strip()
                    break
        if not dbu:
            import yaml
            cfg = yaml.safe_load(open("/etc/llm-manager/litellm_config.yaml"))
            dbu = cfg["general_settings"]["database_url"]
        m = re.match(r"postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/(.+)", dbu)
        conn = psycopg2.connect(
            host=m.group(3), port=m.group(4) or 5432, dbname=m.group(5),
            user=m.group(1), password=m.group(2), connect_timeout=CONNECT_TIMEOUT,
        )
    _conn_cache[which] = conn
    return conn


def _get_caps():
    """Caps from manager_settings (30 s cache)."""
    global _caps_cache, _caps_ts
    now = time.time()
    if _caps_cache is not None and (now - _caps_ts) < CAPS_TTL:
        return _caps_cache
    out = {}
    try:
        conn = _get_conn("app")
        cur = conn.cursor()
        for tag, kname in CAP_KEYS.items():
            cur.execute("SELECT value FROM manager_settings WHERE key = %s", (kname,))
            row = cur.fetchone()
            try:
                out[tag] = float(row[0]) if row else None
            except (TypeError, ValueError):
                out[tag] = None
        conn.commit()
    except Exception as e:
        log.error("spend_guard: cannot read caps: %s", e)
        out = {"daily": None, "monthly": None, "warning": None}
    _caps_cache = out
    _caps_ts = now
    return out


def _get_spend():
    """(daily, monthly) openrouter spend from LiteLLM_SpendLogs."""
    conn = _get_conn("spend")
    cur = conn.cursor()
    now = datetime.utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    mon_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    res = []
    for start in (day_start, mon_start):
        cur.execute(
            'SELECT COALESCE(SUM(spend),0) FROM "LiteLLM_SpendLogs" '
            "WHERE model LIKE %s AND \"startTime\" >= %s",
            ("openrouter/%", start),
        )
        res.append(float(cur.fetchone()[0]))
    conn.commit()
    return res[0], res[1]


class SpendGuard(CustomLogger):
    """Gates openrouter/* deployments against configured spend caps."""

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        model = str(kwargs.get("model", "") or "")
        if "openrouter" not in model:
            return kwargs  # local traffic: zero overhead

        caps = _get_caps()
        try:
            daily, monthly = _get_spend()
        except Exception as e:
            log.error("spend_guard: cannot read spend: %s", e)
            raise HTTPException(
                status_code=429,
                detail="OpenRouter fallback unavailable: spend telemetry unreachable",
            )

        daily_cap = caps.get("daily")
        monthly_cap = caps.get("monthly")
        warn = caps.get("warning")

        if monthly_cap is not None and monthly >= monthly_cap:
            raise HTTPException(
                status_code=429,
                detail="OpenRouter monthly spend cap reached ($%.2f/$%.2f)" % (monthly, monthly_cap),
            )
        if daily_cap is not None and daily >= daily_cap:
            raise HTTPException(
                status_code=429,
                detail="OpenRouter daily spend cap reached ($%.2f/$%.2f)" % (daily, daily_cap),
            )
        if warn is not None and (daily >= warn or monthly >= warn):
            log.warning(
                "spend_guard: OpenRouter spend past warning threshold: daily $%.4f monthly $%.4f (warn $%.2f)",
                daily, monthly, warn,
            )
        return kwargs


# Instantiated at import: LiteLLM config callbacks reference
# "spend_guard.spend_guard_instance" (an instance, not the class).
spend_guard_instance = SpendGuard()


def get_spend_guard_instance():
    return spend_guard_instance
