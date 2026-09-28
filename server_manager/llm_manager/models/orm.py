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

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, func
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


class DeploymentRevisionRecord(LlmBase):
    """public.deployment_revisions — the rollback/backbone of LLM Manager history
    (TDR-0008 slice 2). One row per known-good launch configuration; exact
    rollback payloads live in the row itself (launch_command, systemd_unit)."""

    __tablename__ = "deployment_revisions"

    revision_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_label: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False, index=True)
    artifact: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    quantization: Mapped[str | None] = mapped_column(String, nullable=True)
    runtime: Mapped[str | None] = mapped_column(String, nullable=True)
    runtime_version: Mapped[str | None] = mapped_column(String, nullable=True)
    host: Mapped[str | None] = mapped_column(String, nullable=True)
    vm: Mapped[str | None] = mapped_column(String, nullable=True)
    guest_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    cpu_cores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ram_gb: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ram_speed: Mapped[str | None] = mapped_column(String, nullable=True)
    gpu_model: Mapped[str | None] = mapped_column(String, nullable=True)
    gpu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parallelism: Mapped[str | None] = mapped_column(String, nullable=True)
    max_num_seqs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kv_cache: Mapped[str | None] = mapped_column(String, nullable=True)
    launch_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    systemd_unit: Mapped[str | None] = mapped_column(String, nullable=True)
    aliases: Mapped[str | None] = mapped_column(String, nullable=True)
    # CANDIDATE | ACTIVE | SUPERSEDED | ... (app-defined vocabulary)
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="CANDIDATE")
    visible_by_default: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    preset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeploymentRecord(LlmBase):
    """public.deployments — the currently configured serving entries per host."""

    __tablename__ = "deployments"

    deployment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    served_model: Mapped[str | None] = mapped_column(String, nullable=True)
    max_model_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    config_version: Mapped[str | None] = mapped_column(String, nullable=True)
    desired_service_state: Mapped[str] = mapped_column(String, nullable=False, default="SERVING")


class BenchmarkRunRecord(LlmBase):
    """public.benchmark_runs — benchmark results WITH deployment context
    (TDR-0008 slice 3; §6 benchmark discipline: no context-free numbers)."""

    __tablename__ = "benchmark_runs"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    preset: Mapped[str | None] = mapped_column(String, nullable=True)
    concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    output_tok_s: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ttft_avg_s: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ttft_max_s: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    latency_avg_s: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    gpu_watts_avg: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    deployment_revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    aggregate_output_tok_s: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ram_used_gb: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    vram_used_gb: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    tested_context: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_result_path: Mapped[str | None] = mapped_column(String, nullable=True)
    hermes_tool_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # COMPLETED | FAILED | ... (failed runs are recorded, never hidden)
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="COMPLETED")


class QualificationEnvelopeRecord(LlmBase):
    """public.qualification_envelopes — qualified concurrency/context envelope
    per deployment, linked to the benchmark_run that proved it."""

    __tablename__ = "qualification_envelopes"

    envelope_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deployment_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    recommended_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hard_concurrency_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qualified_context: Mapped[int | None] = mapped_column(Integer, nullable=True)
    benchmark_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kv_cache_dtype: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
