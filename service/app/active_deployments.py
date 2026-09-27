"""active_deployments — §21.7 DB-backed source of truth for active placement.

Replaces hard-coded Python constants as the authority for "which VM is the
active deployment on this physical host". All consumers (dashboard/status API,
health probes, recovery, VM power control, registry sync, deployment-control
APIs, hosting-command presets, routing) derive the active target from here.

Data model (per plan §21.7):

    physical host      active_hosts      physical_host TEXT PRIMARY KEY
    -> active deployment                 active_host_ip TEXT NOT NULL
    -> active VM                         updated_at, updated_by

Usage:
    from active_deployments import active_ip_for, set_active, ACTIVE_HOSTS_TABLE

The `hosts` table remains the full inventory; `active_hosts` only records WHICH
inventory row is active per physical host. Historical deployment revisions and
RDMA ring data are never touched by this module (§21.4).

Fallback behavior: if the table/DB is unavailable, consumers fall back to the
legacy module-level maps so the system degrades to v011 behavior instead of
breaking (documented TDR).
"""
import os
import threading
from datetime import datetime, timezone

_CACHE = {}
_CACHE_TTL_S = 30.0
_cache_lock = threading.Lock()

DDL = """
CREATE TABLE IF NOT EXISTS active_hosts (
    physical_host TEXT PRIMARY KEY,
    active_host_ip TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now(),
    updated_by TEXT
)"""


def _pg():
    """Connection for active_hosts. Honors LLM_MANAGER_PG_DSN (deploy/CI/test),
    else falls back to the shared v011_core.pg() production connector."""
    dsn = os.environ.get("LLM_MANAGER_PG_DSN")
    if dsn:
        try:
            import psycopg2
            return psycopg2.connect(dsn, connect_timeout=5)
        except Exception:
            return None
    try:
        from v011_core import pg  # noqa: local import by design
        return pg()
    except Exception:
        return None


def ensure_table(conn=None):
    own = conn is None
    conn = conn or _pg()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        if own:
            conn.close()


def active_ip_for(physical_host, fallback=None):
    """Return the active guest IP for a physical host, from the DB.

    Falls back to `fallback` (legacy constant) when DB unavailable/empty.
    """
    now = datetime.now(timezone.utc).timestamp()
    with _cache_lock:
        hit = _CACHE.get(physical_host)
        if hit and now - hit[1] < _CACHE_TTL_S:
            return hit[0]
    conn = _pg()
    ip = None
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT active_host_ip FROM active_hosts WHERE physical_host=%s",
                            (physical_host,))
                row = cur.fetchone()
                ip = row[0] if row else None
        except Exception:
            ip = None
        finally:
            conn.close()
    if ip is None:
        ip = fallback
    with _cache_lock:
        _CACHE[physical_host] = (ip, now)
    return ip


def set_active(physical_host, guest_ip, updated_by="system"):
    """Promote a guest IP to active for a physical host (DB update only —
    no Python source edits; §21.7 desired behavior)."""
    conn = _pg()
    if not conn:
        return {"error": "pg unavailable"}
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("""
                INSERT INTO active_hosts (physical_host, active_host_ip, updated_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (physical_host) DO UPDATE SET
                    active_host_ip = EXCLUDED.active_host_ip,
                    updated_at = now(),
                    updated_by = EXCLUDED.updated_by""",
                        (physical_host, guest_ip, updated_by))
        conn.commit()
        with _cache_lock:
            _CACHE.pop(physical_host, None)
        return {"status": "ok", "physical_host": physical_host, "active_host_ip": guest_ip}
    except Exception as e:
        conn.rollback()
        return {"error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


def all_active():
    """Every physical host's active mapping (for dashboards/tests)."""
    conn = _pg()
    out = {}
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT physical_host, active_host_ip FROM active_hosts ORDER BY 1")
                out = dict(cur.fetchall())
        except Exception:
            out = {}
        finally:
            conn.close()
    return out
