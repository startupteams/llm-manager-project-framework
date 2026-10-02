"""ARM database models — Agent Runtime Manager (greenfield, SQLAlchemy 2.x).

Owns (§7): runtime identity, acms_agent_id correlation, provider, VMID,
desired/actual runtime state, health, provisioning jobs/steps, ownership
metadata, restart history, harness/bridge metadata, model-route reference,
runtime audit. Does NOT own semantic work/project/context history (§6/§7).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RuntimeState(str, enum.Enum):
    DESIRED_RUNNING = "DESIRED_RUNNING"
    DESIRED_STOPPED = "DESIRED_STOPPED"
    DESIRED_DESTROYED = "DESIRED_DESTROYED"


class ActualState(str, enum.Enum):
    ABSENT = "ABSENT"            # no VM created yet
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    DESTROYED = "DESTROYED"
    SUPERSEDED = "SUPERSEDED"    # historical failed attempt; superseded by a live runtime (never reconciled)


class JobState(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentRuntime(Base):
    """A replaceable runtime identity underneath an ACMS persistent agent (§1)."""

    __tablename__ = "agent_runtimes"

    runtime_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    acms_agent_id: Mapped[str] = mapped_column(String(64), index=True)  # correlation UUID from ACMS, no FK (§1)
    provisioning_request_id: Mapped[str] = mapped_column(String(64), index=True)  # idempotency key from ACMS
    name: Mapped[str] = mapped_column(String(80))

    provider: Mapped[str] = mapped_column(String(40), default="proxmox_vm")
    node: Mapped[str | None] = mapped_column(String(40), nullable=True)
    vmid: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    harness: Mapped[str] = mapped_column(String(40), default="hermes")
    runtime_class: Mapped[str] = mapped_column(String(40), default="software_development_worker")
    site: Mapped[str] = mapped_column(String(40), default="marion-ia-usa")
    model_route: Mapped[str | None] = mapped_column(String(80), nullable=True)

    desired_state: Mapped[RuntimeState] = mapped_column(Enum(RuntimeState, name="arm_runtime_desired_state"),
                                                        default=RuntimeState.DESIRED_RUNNING)
    actual_state: Mapped[ActualState] = mapped_column(Enum(ActualState, name="arm_runtime_actual_state"),
                                                      default=ActualState.ABSENT)

    # §8 ownership metadata — REQUIRED before any destructive action.
    # MutableDict: the service mutates this dict in place; without mutation
    # tracking SQLAlchemy never flags the attribute dirty and the mutations
    # are silently dropped on commit.
    created_by_agent_runtime_manager: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    template_source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ownership_meta: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSONB), default=dict)

    # §10A Hermes state backup metadata
    hermes_state_repo: Mapped[str | None] = mapped_column(String(120), nullable=True)
    hermes_state_branch: Mapped[str | None] = mapped_column(String(120), nullable=True)
    hermes_profile_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_state_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state_sync_health: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # §10B reconciler bookkeeping
    last_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # §26 sandbox TTL — set when runtime_class="sandbox"; the reconciler sweep
    # flips expired sandboxes to DESIRED_DESTROYED (API-only; nothing is
    # auto-deleted from PVE).
    sandbox_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    jobs: Mapped[list["ProvisioningJob"]] = relationship(back_populates="runtime", lazy="selectin")


class ProvisioningJob(Base):
    """Async provisioning job with idempotency (§10/§13)."""

    __tablename__ = "provisioning_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # idempotency key
    runtime_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runtimes.runtime_id", ondelete="CASCADE"))
    requested_by: Mapped[str] = mapped_column(String(60))  # service identity
    authority: Mapped[str] = mapped_column(String(60), default="sprint_execution_context")  # §14
    state: Mapped[JobState] = mapped_column(Enum(JobState, name="arm_job_state"), default=JobState.QUEUED)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    runtime: Mapped[AgentRuntime] = relationship(back_populates="jobs")
    steps: Mapped[list["ProvisioningStep"]] = relationship(back_populates="job", lazy="selectin",
                                                           order_by="ProvisioningStep.seq")


class ProvisioningStep(Base):
    __tablename__ = "provisioning_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provisioning_jobs.job_id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    step: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(20), default="RUNNING")  # RUNNING/DONE/FAILED
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[ProvisioningJob] = relationship(back_populates="steps")
    __table_args__ = (UniqueConstraint("job_id", "seq", name="uq_provstep_job_seq"),)


class RuntimeEvent(Base):
    """Runtime audit (§8: every destructive action records an audit event)."""

    __tablename__ = "runtime_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    runtime_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60))
    result: Mapped[str] = mapped_column(String(20), default="ok")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BridgeRegistration(Base):
    """Agent Bridge registration correlation (§15): runtime ↔ ACMS identity link."""

    __tablename__ = "bridge_registrations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    runtime_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runtimes.runtime_id", ondelete="CASCADE"))
    acms_agent_id: Mapped[str] = mapped_column(String(64), index=True)
    bridge_credential_id: Mapped[str] = mapped_column(String(80))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("runtime_id", "acms_agent_id", name="uq_bridge_reg_runtime_agent"),)


# Ownership-metadata keys every Runtime-Manager-owned VM MUST carry (§8)
OWNERSHIP_REQUIRED_KEYS = (
    "server_runtime_id",
    "acms_agent_id",
    "provisioning_request_id",
    "provider",
    "node",
    "vmid",
    "created_by_agent_runtime_manager",
    "created_at",
    "template_source",
)
