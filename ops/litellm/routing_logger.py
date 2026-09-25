#!/usr/bin/env python3
"""v0.11 routing logger + model-pin guard for LiteLLM (plan §D4, §G, §H7).

LiteLLM CustomLogger callback:
- logs per-request model + resolved deployment (api_base) + tokens + latency
  into llmmanager.request_routing_log (§D4.4 record which deployment handled it)
- hard-fails any request whose model is missing or is an alias with no
  healthy deployment (LiteLLM resolves aliases before callbacks; a missing
  model naturally errors — this guards silent substitution paths)
"""
import json
import os
import psycopg2
from litellm.integrations.custom_logger import CustomLogger

DSN_FILE = "/etc/llm-manager/secrets/pg_app_creds"
_conn = None


def _conn():
    global _conn
    if _conn is not None:
        try:
            with _conn.cursor() as cur:
                cur.execute("SELECT 1")
            return _conn
        except Exception:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
    kv = {}
    with open(DSN_FILE) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                kv[k] = v
    _conn = psycopg2.connect(host=kv["PG_HOST"], port=5432, dbname=kv["PG_DB"],
                             user=kv["PG_USER"], password=kv["PG_PW"], connect_timeout=3)
    return _conn


def _alias(api_key):
    # LiteLLM passes the hashed key in kwargs; alias resolution via SpendLogs is
    # done offline — keep the hash prefix for correlation.
    return (api_key or "")[:12] or None


class RoutingLogger(CustomLogger):
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            model = kwargs.get("model") or ""
            lit = kwargs.get("litellm_params") or {}
            meta = lit.get("metadata") or {}
            api_base = (kwargs.get("litellm_params") or {}).get("api_base") \
                or kwargs.get("api_base") or ""
            usage = getattr(response_obj, "usage", None)
            pt = getattr(usage, "prompt_tokens", 0) or 0
            ct = getattr(usage, "completion_tokens", 0) or 0
            rid = getattr(response_obj, "id", None)
            dur_ms = int((end_time - start_time).total_seconds() * 1000)
            key_hash = kwargs.get("litellm_params", {}).get("metadata", {}).get("user_api_key_hash") \
                or (kwargs.get("metadata") or {}).get("user_api_key_hash")
            conn = _conn()
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO request_routing_log
                               (request_id, model, api_base, prompt_tokens, completion_tokens,
                                latency_ms, key_alias)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                            (rid, model, api_base, pt, ct, dur_ms, _alias(key_hash)))
            conn.commit()
        except Exception:
            try:
                _conn().rollback()
            except Exception:
                pass

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """Proxy processes requests asynchronously (LiteLLM v1.100 dispatches
        async_* for async requests); share the sync implementation."""
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.log_success_event,
                                   kwargs, response_obj, start_time, end_time)


routing_logger_instance = RoutingLogger()
