"""LLM Manager ORM — slice 4 (TDR-0008, 2026-09-29): read-only reflective models
for tables NOT gated by the pending gpu_samples retention decision.

Mapped exactly from the live llmmanager schema (verified 2026-09-29 via
information_schema on prod): recovery_events, request_routing_log,
electricity_rates, manager_settings.

gpu_samples / facility_power_samples / host_power_rollup_1m / facility_power_rollup_1d /
emporia_* / rdma_ring_nodes / multi_node_deployments are deliberately NOT modeled here —
telemetry family waits for the human retention decision (TDR-0008 slice log). No DDL is
introduced; write paths stay on psycopg2 (TDR-0008 rule).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .orm import LlmBase


class RecoveryEventRecord(LlmBase):
    """public.recovery_events — audit of the recovery engine's actions.

    NOTE (2026-09-24/25 lessons, encoded in v011_recovery): desired_power_state
    STOPPED still fires recovery; only STOPPED_INTENTIONAL leaves a VM off.
    This model mirrors history; it does not interpret it.
    """

    __tablename__ = "recovery_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RequestRoutingLogRecord(LlmBase):
    """public.request_routing_log — per-request routing/usage metadata
    (metadata-first policy: no prompt/response bodies)."""

    __tablename__ = "request_routing_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    api_base: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    key_alias: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_spend: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)


class ElectricityRateRecord(LlmBase):
    """public.electricity_rates — versioned rate components (Rate 400 base +
    year-round riders adder; §16 component model). Calculations must use the
    componentized record effective at consumption time — never a hardcoded all-in rate."""

    __tablename__ = "electricity_rates"

    rate_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component: Mapped[str] = mapped_column(String, nullable=False)
    value_per_kwh: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    season: Mapped[str] = mapped_column(String, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ManagerSettingRecord(LlmBase):
    """public.manager_settings — app key/value settings (redacted in any UI)."""

    __tablename__ = "manager_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
