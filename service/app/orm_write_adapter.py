"""ORM Slice 5 write-path adapter for the v0.11 app (TDR-0008; plan Phase E).

Cutover contract: the legacy psycopg2 write sites for hosts / model_registry /
recovery_events route through THIS module (sync SQLAlchemy 2 over the SAME
psycopg2 credential contract). Semantics mirror the legacy SQL exactly —
same columns, same WHERE, same updated_at handling — so a row-for-row diff
after cutover is the proof (parity probe at the bottom).

Rules honored:
- NO schema change, NO DDL (TDR-0008 no-DDL invariant; tables pre-exist).
- LiteLLM-owned tables are NEVER touched here.
- Legacy behavior documented per function (comment: "legacy: ...").
- A cutover failure raises — the caller keeps its legacy error semantics.
- engine is created lazily and cached per-process (single-replica discipline).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.orm import Session

_lock = threading.Lock()
_engine = None
_tables: dict[str, Table] = {}

# Columns the adapter is ALLOWED to write (schema-honesty guard: any attempt
# to write an unknown column fails here, not at the DB).
_WRITABLE = {
    "hosts": {"node", "vmid", "desired_power_state", "management_mode"},
    "model_registry": {"logical_model_name", "host_id", "engine", "backend_url",
                        "context_limit", "health", "routable", "last_verified",
                        "updated_at", "is_alias", "alias_target"},
    "recovery_events": {"ip", "event_type", "detail"},
}


def _get_engine():
    """Lazy engine using the same credential contract as v011_core.pg()
    (env PG_* first, then /etc/llm-manager/secrets/pg_app_creds PG_PW= line)."""
    global _engine
    with _lock:
        if _engine is not None:
            return _engine
        import os

        host = os.environ.get("PG_HOST") or os.environ.get("LLM_MANAGER_PG_HOST") or "127.0.0.1"
        port = os.environ.get("PG_PORT", "5432")
        dbname = os.environ.get("PG_DB", "llmmanager")
        user = os.environ.get("PG_USER", "llmmanager")
        pw = os.environ.get("PG_PW")
        if pw is None:
            pw_file = os.environ.get("PG_PW_FILE", "/etc/llm-manager/secrets/pg_app_creds")
            try:
                with open(pw_file) as f:
                    for line in f:
                        if line.startswith("PG_PW="):
                            pw = line.strip().split("=", 1)[1]
                            break
            except OSError:
                pass
        if pw is None:
            raise RuntimeError("orm_write_adapter: no PG credentials (same contract as v011_core.pg)")
        # Socket-dir hosts (pgserver tests): a path in the netloc is parsed as
        # the DB path — emit as ?host= query instead (documented trap).
        if host.startswith("/"):
            _engine = create_engine(
                f"postgresql+psycopg2://{user}:{pw}@/{dbname}?host={host}",
                pool_pre_ping=True, pool_size=4, max_overflow=2, future=True)
        else:
            _engine = create_engine(
                f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{dbname}",
                pool_pre_ping=True, pool_size=4, max_overflow=2, future=True)
        return _engine


def _table(name: str) -> Table:
    """Reflect the table ONCE per process; no DDL, metadata read-only."""
    if name not in _tables:
        md = MetaData()
        with _get_engine().connect() as conn:
            _tables[name] = Table(name, md, autoload_with=conn, schema="public")
    return _tables[name]


def _guard(table: str, values: dict) -> dict:
    bad = set(values) - _WRITABLE.get(table, set())
    if bad:
        raise ValueError(f"orm_write_adapter: columns not writable for {table}: {sorted(bad)}")
    return values


# ------------------------------------------------------------------- hosts

def orm_update_hosts_by_ip(*, guest_ip: str, fields: dict) -> int:
    """legacy: UPDATE hosts SET <fields> WHERE guest_ip=%s → rowcount."""
    fields = _guard("hosts", dict(fields))
    if not fields:
        return 0
    t = _table("hosts")
    with _get_engine().begin() as conn:
        result = conn.execute(t.update().where(t.c.guest_ip == guest_ip).values(**fields))
        return result.rowcount or 0


def orm_hosts_row_by_ip(guest_ip: str) -> dict | None:
    """legacy: SELECT guest_ip, desired_power_state, management_mode FROM hosts WHERE guest_ip=%s."""
    import sqlalchemy as sa

    t = _table("hosts")
    with _get_engine().connect() as conn:
        row = conn.execute(
            sa.select(t.c.guest_ip, t.c.desired_power_state, t.c.management_mode)
            .where(t.c.guest_ip == guest_ip)
        ).mappings().first()
    if row is None:
        return None
    return {"guest_ip": row["guest_ip"], "desired_power_state": row["desired_power_state"],
            "management_mode": row["management_mode"]}


def orm_hosts_inventory() -> list[dict]:
    """legacy: SELECT host_id, guest_ip, name FROM hosts ORDER BY host_id."""
    import sqlalchemy as sa

    t = _table("hosts")
    with _get_engine().connect() as conn:
        rows = conn.execute(sa.select(t.c.host_id, t.c.guest_ip, t.c.name)
                            .order_by(t.c.host_id)).mappings().all()
    return [{"host_id": r["host_id"], "guest_ip": r["guest_ip"], "name": r["name"]}
            for r in rows]


def orm_host_flags(host_id: int) -> tuple[str, str]:
    """legacy: SELECT desired_power_state, management_mode FROM hosts WHERE host_id=%s."""
    import sqlalchemy as sa

    t = _table("hosts")
    with _get_engine().connect() as conn:
        row = conn.execute(sa.select(t.c.desired_power_state, t.c.management_mode)
                           .where(t.c.host_id == host_id)).mappings().first()
    return (row["desired_power_state"] if row else "RUNNING",
            row["management_mode"] if row else "MANAGED")


# ---------------------------------------------------------- model_registry

def orm_upsert_model_registry(*, logical_model_name: str, host_id, engine: str,
                              backend_url: str, context_limit, health: str,
                              routable: bool, is_alias: bool = False,
                              alias_target=None) -> None:
    """legacy sync_registry upsert (v011_core:251-263 + alias 283-288).

    Real models: ON CONFLICT (logical_model_name, host_id) DO UPDATE SET
      engine/backend_url/context_limit/health/routable/last_verified/updated_at.
    Aliases: host_id NULL; ON CONFLICT DO NOTHING (legacy wording).
    """
    t = _table("model_registry")
    now = datetime.now(timezone.utc)
    values = {
        "logical_model_name": logical_model_name, "host_id": host_id,
        "engine": engine, "backend_url": backend_url,
        "context_limit": context_limit, "health": health, "routable": bool(routable),
        "last_verified": now, "updated_at": now,
        "is_alias": bool(is_alias), "alias_target": alias_target,
    }
    values = _guard("model_registry", values)
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = pg_insert(t).values(**values)
    if is_alias:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["logical_model_name", "host_id"])
    else:
        stmt = stmt.on_conflict_do_update(
            index_elements=["logical_model_name", "host_id"],
            set_={c: values[c] for c in ("engine", "backend_url", "context_limit",
                                          "health", "routable", "last_verified",
                                          "updated_at")})
    with _get_engine().begin() as conn:
        conn.execute(stmt)


def orm_mark_host_unroutable(*, host_id: int, health: str) -> int:
    """legacy (v011_core:266): UPDATE model_registry SET health=%s, routable=FALSE,
    updated_at=now() WHERE host_id=%s."""
    t = _table("model_registry")
    with _get_engine().begin() as conn:
        result = conn.execute(
            t.update().where(t.c.host_id == host_id)
            .values(health=health, routable=False,
                    updated_at=datetime.now(timezone.utc)))
        return result.rowcount or 0


def orm_delete_aliases() -> int:
    """legacy (v011_core:274): DELETE FROM model_registry WHERE is_alias=TRUE."""
    t = _table("model_registry")
    with _get_engine().begin() as conn:
        result = conn.execute(t.delete().where(t.c.is_alias == True))  # noqa: E712
        return result.rowcount or 0


def orm_alias_target_routable(target: str) -> bool:
    """legacy (v011_core:280): SELECT COUNT(*) ... WHERE name=%s AND routable=TRUE."""
    import sqlalchemy as sa

    t = _table("model_registry")
    with _get_engine().connect() as conn:
        n = conn.execute(sa.select(sa.func.count()).select_from(t).where(
            t.c.logical_model_name == target, t.c.routable == True)).scalar()  # noqa: E712
    return (n or 0) > 0


def orm_routable_names() -> list[str]:
    """legacy routable_models(): SELECT DISTINCT name WHERE routable=TRUE."""
    import sqlalchemy as sa

    t = _table("model_registry")
    with _get_engine().connect() as conn:
        rows = conn.execute(sa.select(t.c.logical_model_name).distinct()
                            .where(t.c.routable == True)).all()  # noqa: E712
    return sorted(r[0] for r in rows)


# ---------------------------------------------------------- recovery_events

def orm_insert_recovery_event(*, ip: str, event_type: str, detail) -> None:
    """legacy: INSERT INTO recovery_events (ip, event_type, detail) VALUES (%s,%s,%s).
    dict detail is JSON-encoded exactly like every legacy call site."""
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail)
    with _get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO recovery_events (ip, event_type, detail) "
            "VALUES (:ip, :event_type, :detail)"),
            {"ip": ip, "event_type": event_type, "detail": detail})


def orm_recent_event_count(*, ip: str, event_type: str, minutes: int = 30) -> int:
    """legacy recent_count(): COUNT(*) WHERE ip/event_type AND created_at > now()-interval."""
    with _get_engine().connect() as conn:
        return conn.execute(text(
            "SELECT COUNT(*) FROM recovery_events WHERE ip=:ip AND event_type=:et "
            "AND created_at > now() - (:m || ' minutes')::interval"),
            {"ip": ip, "et": event_type, "m": str(minutes)}).scalar() or 0


# ------------------------------------------------------------------ parity

def parity_probe(limit: int = 500) -> dict:
    """Row-count + content-checksum parity probe (TDR-0008 §6 contract)."""
    out = {}
    with _get_engine().connect() as conn:
        for table in ("hosts", "model_registry", "recovery_events"):
            n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            probe_n = conn.execute(
                text(f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT :l) t"),
                {"l": limit}).scalar()
            checksum = conn.execute(text(
                f"SELECT md5(coalesce(string_agg(t.x::text, ',' ORDER BY t.x), '')) "
                f"FROM (SELECT to_jsonb(t2.*)::text x FROM {table} t2 LIMIT :l) t"),
                {"l": limit}).scalar()
            out[table] = {"rows": n, "probe_rows": probe_n, "checksum": checksum}
    return out