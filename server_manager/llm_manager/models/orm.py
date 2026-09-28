"""LLM Manager ORM models — slice 1 (TDR-0008; flight plan Phase 5).

REFLECTIVE models: they map the EXISTING llmmanager schema exactly (hosts,
active_hosts, model_registry). The llmmanager DB schema remains owned by the
db/migrations SQL ledger — these models introduce NO DDL and must never drift
from schema.sql (validated by row-count/content parity tests).

Write paths STAY on the proven psycopg2 app code this slice; these models are
for read parity + future cutover behind server_manager.llm_manager.adapters.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class LlmBase(DeclarativeBase):
    """Declarative base for the llmmanager DB (reflective models only)."""


class HostRecord(LlmBase):
    """public.hosts — physical compute hosts LLM Manager deploys to.

    NOTE: recovery semantics (desired_power_state/desired_service_state,
    STOPPED vs STOPPED_INTENTIONAL) live in the recovery engine; the ORM is a
    mirror and must not become a second write authority without parity tests.
    """

    __tablename__ = "hosts"

    host_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    guest_ip: Mapped[str] = mapped_column(String, nullable=False)
    gpu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_model: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    desired_power_state: Mapped[str] = mapped_column(String, nullable=False, default="RUNNING")
    management_mode: Mapped[str] = mapped_column(String, nullable=False, default="MANAGED")
    node: Mapped[str | None] = mapped_column(String, nullable=True)
    vmid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    desired_service_state: Mapped[str] = mapped_column(String, nullable=False, default="SERVING")


class ActiveHostRecord(LlmBase):
    """public.active_hosts — which physical hosts currently serve model traffic
    (promotion = DB update followed by sync_registry; §21.7 contract).

    Live schema (verified 2026-09-28): physical_host text NOT NULL (PK),
    active_host_ip text NOT NULL, updated_at timestamptz DEFAULT now(),
    updated_by text.
    """

    __tablename__ = "active_hosts"

    physical_host: Mapped[str] = mapped_column(String, primary_key=True)
    active_host_ip: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)


class ModelRegistryRecord(LlmBase):
    """public.model_registry — logical model → backend mapping (the routes)."""

    __tablename__ = "model_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    logical_model_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    host_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engine: Mapped[str | None] = mapped_column(String, nullable=True)
    backend_url: Mapped[str | None] = mapped_column(String, nullable=True)
    context_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capabilities: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    health: Mapped[str | None] = mapped_column(String, nullable=True, default="unknown")
    routable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    is_alias: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    alias_target: Mapped[str | None] = mapped_column(String, nullable=True)
    last_verified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
