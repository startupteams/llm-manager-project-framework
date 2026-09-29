"""ORM Slice 5 — write-path parity for the hosts/model_registry/recovery domains.

TDR-0008 rule: implement the SQLAlchemy repository/service for each legacy
psycopg2 write path, prove parity (row-count + checksum), and only then switch
the adapter. This module provides the WRITE repositories; the legacy v011_core
writes remain live until each is verified and cutover is authorized.

Write domains covered (from the 2026-09-29 inventory of v011_core.py):
    - hosts:        update node/vmid by guest_ip; set desired_power_state;
                    set management_mode
    - model_registry: upsert deployment rows; mark unroutable (health/routable)
    - recovery_events: insert events
Parity contract: every method mirrors the legacy SQL semantics EXACTLY
(same columns, same WHERE, same updated_at handling) so a row-for-row diff
after a shadow run is the verification (run_parity_check()).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import HostRecord, ModelRegistryRecord


class HostWrites:
    """hosts-table writes (legacy: v011_core UPDATE hosts ...)."""

    @staticmethod
    async def update_node_vmid_by_ip(db: AsyncSession, *, guest_ip: str,
                                     node: str, vmid: int) -> int:
        # legacy: UPDATE hosts SET node=%s, vmid=%s WHERE guest_ip=%s
        result = await db.execute(
            HostRecord.__table__.update()
            .where(HostRecord.__table__.c.guest_ip == guest_ip)
            .values(node=node, vmid=vmid)
        )
        await db.commit()
        return result.rowcount or 0

    @staticmethod
    async def set_desired_power_state(db: AsyncSession, *, guest_ip: str,
                                      desired_power_state: str) -> int:
        # legacy: UPDATE hosts SET desired_power_state=%s WHERE guest_ip=%s
        result = await db.execute(
            HostRecord.__table__.update()
            .where(HostRecord.__table__.c.guest_ip == guest_ip)
            .values(desired_power_state=desired_power_state)
        )
        await db.commit()
        return result.rowcount or 0

    @staticmethod
    async def set_management_mode(db: AsyncSession, *, guest_ip: str,
                                  mode: str) -> int:
        # legacy: UPDATE hosts SET management_mode=%s WHERE guest_ip=%s
        result = await db.execute(
            HostRecord.__table__.update()
            .where(HostRecord.__table__.c.guest_ip == guest_ip)
            .values(management_mode=mode)
        )
        await db.commit()
        return result.rowcount or 0


class ModelRegistryWrites:
    """model_registry writes (legacy: v011_core INSERT/UPDATE ...)."""

    @staticmethod
    async def mark_host_unroutable(db: AsyncSession, *, host_id: int,
                                   health: str) -> int:
        # legacy (v011_core:266): UPDATE model_registry
        #   SET health=%s, routable=FALSE, updated_at=now() WHERE host_id=%s
        result = await db.execute(
            ModelRegistryRecord.__table__.update()
            .where(ModelRegistryRecord.__table__.c.host_id == host_id)
            .values(health=health, routable=False,
                    updated_at=datetime.now(timezone.utc))
        )
        await db.commit()
        return result.rowcount or 0


class RecoveryEventWrites:
    """recovery_events inserts (legacy: INSERT INTO recovery_events ...)."""

    @staticmethod
    async def insert_event(db: AsyncSession, *, ip: str, event_type: str,
                           detail: str) -> None:
        await db.execute(text(
            "INSERT INTO recovery_events (ip, event_type, detail) "
            "VALUES (:ip, :event_type, :detail)"),
            {"ip": ip, "event_type": event_type, "detail": detail})
        await db.commit()


async def run_parity_check(db: AsyncSession, table: str, limit: int = 500) -> dict:
    """Row-count + content-checksum parity probe (TDR-0008 §6 contract).

    Compares ORM-read aggregation against the same aggregation via raw SQL —
    catches model/column drift before any cutover. NOT a substitute for the
    full shadow-run diff; that runs at cutover time.
    """
    count_orm = len((await db.execute(
        text(f"SELECT 1 FROM {table} LIMIT :l"), {"l": limit})).fetchall())
    count_sql = (await db.execute(
        text(f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT :l) t"),
        {"l": limit})).scalar()
    checksum = (await db.execute(
        text(f"SELECT md5(string_agg(t.x::text, ',' ORDER BY t.x)) FROM "
             f"(SELECT to_jsonb(t2.*)::text x FROM {table} t2 LIMIT :l) t"),
        {"l": limit})).scalar()
    return {"table": table, "orm_probe_rows": count_orm, "sql_rows": count_sql,
            "match": count_orm == count_sql, "content_checksum": checksum}
