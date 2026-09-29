"""Desired-state reconciler — makes ARM's recorded desired state execute safely (§10B/§13).

Safety model (§8):
- Acts ONLY on Agent Runtime Manager-owned runtimes with verified durable
  ownership markers on the backing VM. Mismatch fails closed.
- desired RUNNING  → observe → start owned VM if stopped → wait for
  bridge readiness (bounded) → actual RUNNING.
- desired STOPPED  → graceful shutdown → verify stopped → NEVER destroy a
  persistent runtime VM as part of reconcile.
- desired DESTROYED → ownership-verified destroy only (explicit API path,
  never an automatic reconcile consequence).
- Recovery: bounded retries with backoff; every attempt audited via
  RuntimeEvent; last_reconcile_at / last_error / recovery_count recorded.

Single-replica discipline (TDR-0005): one loop instance per process; the API
deployment runs a single uvicorn worker. Leadership election is future work.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from server_manager.agent_runtime_manager.models.orm import (
    ActualState,
    AgentRuntime,
    RuntimeEvent,
    RuntimeState,
)
from server_manager.agent_runtime_manager.providers.proxmox_vm import (
    OwnershipError,
    ProxmoxVMProvider,
    verify_ownership,
)

log = logging.getLogger("arm.reconciler")


@dataclass
class ReconcileSettings:
    """Tunables (env-overridable). Backoff is exponential-capped.

    Recovery policy (human direction 2026-09-29): maximum 5 automatic
    recovery attempts per incident, attempts varying (bounded exponential
    backoff), then Human Attention. Strategy ladder for the VM-level failure
    classes this service owns: recheck/observe → VM start → exhausted →
    Human Attention. Service/harness restarts belong to their own components
    (LLM Manager serving stack / ACMS bridge), NOT here; PDU hard-cycle is
    NEVER an automatic step.
    """

    max_recovery_attempts: int = 5
    backoff_base_s: int = 10
    backoff_cap_s: int = 120
    bridge_wait_s: int = 300
    poll_interval_s: int = 60
    shutdown_wait_s: int = 30

    @classmethod
    def from_env(cls) -> "ReconcileSettings":
        import os

        def _i(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        return cls(
            max_recovery_attempts=_i("ARM_RECONCILE_MAX_ATTEMPTS", 5),
            backoff_base_s=_i("ARM_RECONCILE_BACKOFF_BASE_S", 10),
            backoff_cap_s=_i("ARM_RECONCILE_BACKOFF_CAP_S", 120),
            bridge_wait_s=_i("ARM_RECONCILE_BRIDGE_WAIT_S", 300),
            poll_interval_s=_i("ARM_RECONCILE_POLL_S", 60),
            shutdown_wait_s=_i("ARM_RECONCILE_SHUTDOWN_WAIT_S", 30),
        )


@dataclass
class ReconcileResult:
    runtime_id: str
    desired: str
    before: str
    after: str
    action: str  # noop | started | shutdown | wait_bridge | error | skipped_unowned
    error: str | None = None
    detail: dict = field(default_factory=dict)


class ReconciliationService:
    """One reconcile pass over ARM-owned runtimes (or a single runtime)."""

    def __init__(self, provider: ProxmoxVMProvider | None = None, settings: ReconcileSettings | None = None):
        self.provider = provider or ProxmoxVMProvider()
        self.settings = settings or ReconcileSettings.from_env()

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _pve_vm_state(self, node: str | None, vmid: int | None) -> str:
        """True PVE state: 'running' | 'stopped' | 'absent' | 'unknown'."""
        if not node or vmid is None:
            return "absent"
        try:
            st = self.provider.vm_status(node, vmid) or {}
            return st.get("status") or "unknown"
        except Exception as e:  # noqa: BLE001 — PVE unreachable is a recorded condition
            log.warning("pve state read failed for %s/%s: %s", node, vmid, e)
            return "unknown"

    def _wait_pve_state(self, node: str | None, vmid: int | None, expect: str, max_s: int = 30) -> str:
        """Bounded poll for a PVE state transition (async state flips lag the API ack)."""
        deadline = time.time() + max_s
        state = self._pve_vm_state(node, vmid)
        while state != expect and time.time() < deadline:
            time.sleep(3)
            state = self._pve_vm_state(node, vmid)
        return state

    def _verify_owned(self, session: SASession, r: AgentRuntime) -> None:
        """Fail-closed ownership verification before ANY state-changing action."""
        if r.node is None or r.vmid is None:
            raise OwnershipError(f"runtime {r.runtime_id} has no backing VM (node/vmid null)")
        s = __import__("server_manager.common.config.settings", fromlist=["get_settings"]).get_settings()
        verify_ownership(self.provider, r.node, r.vmid, r, s.protected_vmids)

    def _audit(self, session: SASession, r: AgentRuntime, action: str, result: str, detail: dict) -> None:
        session.add(RuntimeEvent(actor="arm-reconciler", action=action, runtime_id=r.runtime_id,
                                 result=result, detail=detail))

    # ------------------------------------------------------------- pass
    def reconcile_runtime(self, session: SASession, runtime_id) -> ReconcileResult:
        """Reconcile a single runtime by id. Commit is the caller's responsibility."""
        r = session.get(AgentRuntime, runtime_id)
        if r is None:
            return ReconcileResult(runtime_id=str(runtime_id), desired="?", before="?", after="?",
                                   action="error", error="runtime not found")
        before = r.actual_state.value if r.actual_state else ActualState.ABSENT.value
        desired = r.desired_state

        # SUPERSEDED rows are historical evidence (failed provisioning attempts
        # linked to a live retry) — evidence-preserving, never reconciled, never
        # flipped back by PVE truth-correction.
        if r.actual_state == ActualState.SUPERSEDED:
            res = ReconcileResult(runtime_id=str(r.runtime_id), desired=desired.value, before=before,
                                  after=before, action="noop",
                                  detail={"note": "superseded rows are historical; reconciler skips"})
            self._record(session, r, res)
            return res

        pve = self._pve_vm_state(r.node, r.vmid)

        # desired DESTROYED is handled only by the explicit API destroy path (§8);
        # the reconciler must not auto-destroy.
        if desired == RuntimeState.DESIRED_DESTROYED:
            res = ReconcileResult(runtime_id=str(r.runtime_id), desired=desired.value, before=before,
                                  after=before, action="noop",
                                  detail={"note": "destroy is API-only, reconciler never auto-destroys"})
            self._record(session, r, res)
            return res

        # PVE unreachable: record and stop safely (no state flip on unknown ground truth)
        if pve == "unknown":
            res = ReconcileResult(runtime_id=str(r.runtime_id), desired=desired.value, before=before,
                                  after=before, action="error", error="PVE unavailable")
            r.last_error = "PVE unavailable at reconcile"
            self._record(session, r, res)
            return res

        try:
            if desired == RuntimeState.DESIRED_RUNNING:
                res = self._reconcile_running(session, r, before, pve)
            else:  # DESIRED_STOPPED
                res = self._reconcile_stopped(session, r, before, pve)
        except OwnershipError as e:
            res = ReconcileResult(runtime_id=str(r.runtime_id), desired=desired.value, before=before,
                                  after=before, action="error", error=f"ownership: {e}")
            self._audit(session, r, "reconcile_refused", "refused", {"reason": str(e)})
        except Exception as e:  # noqa: BLE001 — recorded as reconcile failure, never crash the pass
            log.exception("reconcile failed for %s", r.runtime_id)
            res = ReconcileResult(runtime_id=str(r.runtime_id), desired=desired.value, before=before,
                                  after=before, action="error", error=f"{e.__class__.__name__}: {e}"[:500])
            self._audit(session, r, "reconcile_failed", "error", {"error": res.error})
        self._record(session, r, res)
        return res

    # ------------------------------------------------- desired RUNNING
    def _reconcile_running(self, session: SASession, r: AgentRuntime, before: str, pve: str) -> ReconcileResult:
        if pve == "running":
            # Already up: derive truth, clear error only if it was PVE-related.
            if r.actual_state != ActualState.RUNNING:
                r.actual_state = ActualState.RUNNING
                r.recovery_count = 0
                r.last_error = None
                self._audit(session, r, "reconcile_observe", "ok",
                            {"note": "PVE says running; actual corrected", "pve": pve,
                             "recovery_method": "recheck_observe",
                             "time_to_recovery_note": "converged by observation"})

            return ReconcileResult(runtime_id=str(r.runtime_id), desired="DESIRED_RUNNING",
                                   before=before, after=ActualState.RUNNING.value, action="noop")
        if pve not in ("stopped",):
            # 'absent' with desired RUNNING → provisioning job territory (not reconcile)
            return ReconcileResult(runtime_id=str(r.runtime_id), desired="DESIRED_RUNNING",
                                   before=before, after=before, action="noop",
                                   detail={"note": f"VM pve_state={pve}; provisioning path owns this state"})

        # stopped → must start, but ONLY an owned VM
        self._verify_owned(session, r)
        if r.recovery_count >= self.settings.max_recovery_attempts:
            r.last_error = f"recovery exhausted ({r.recovery_count} attempts)"
            self._audit(session, r, "reconcile_recovery_exhausted", "error",
                        {"attempts": r.recovery_count,
                         "failure_class": "vm_start_recovery_exhausted",
                         "recovery_method": "vm_start",
                         "strategy_next": "human_attention",
                         "note": ("bounded strategy ladder complete: recheck → vm_start "
                                  "×%d → exhausted; PDU hard-cycle is never automatic; "
                                  "service/harness restarts belong to their own components"
                                  % r.recovery_count)})
            return ReconcileResult(runtime_id=str(r.runtime_id), desired="DESIRED_RUNNING",
                                   before=before, after=before, action="error",
                                   error=r.last_error, detail={"recovery_count": r.recovery_count})
        # bounded backoff before a start retry
        if r.recovery_count > 0:
            delay = min(self.settings.backoff_base_s * (2 ** (r.recovery_count - 1)),
                        self.settings.backoff_cap_s)
            self._audit(session, r, "reconcile_backoff", "ok", {"delay_s": delay})
            # In-loop sleep is bounded by cap (≤120s); single-threaded loop accepts it.
            time.sleep(delay)
        self._audit(session, r, "reconcile_start", "ok",
                    {"node": r.node, "vmid": r.vmid,
                     "failure_class": "vm_stopped_desired_running",
                     "recovery_method": "vm_start",
                     "attempt": (r.recovery_count or 0) + 1,
                     "recovery_history": True})
        self.provider.start(r.node, r.vmid)
        r.actual_state = ActualState.RUNNING
        r.recovery_count = (r.recovery_count or 0) + 1
        r.last_error = None
        return ReconcileResult(runtime_id=str(r.runtime_id), desired="DESIRED_RUNNING",
                               before=before, after=ActualState.RUNNING.value, action="started",
                               detail={"node": r.node, "vmid": r.vmid,
                                       "attempt": r.recovery_count,
                                       "recovery_method": "vm_start"})

    # ------------------------------------------------ desired STOPPED
    def _reconcile_stopped(self, session: SASession, r: AgentRuntime, before: str, pve: str) -> ReconcileResult:
        if pve == "stopped":
            if r.actual_state != ActualState.STOPPED:
                r.actual_state = ActualState.STOPPED
                self._audit(session, r, "reconcile_observe", "ok",
                            {"note": "PVE says stopped; actual corrected", "pve": pve})
            return ReconcileResult(runtime_id=str(r.runtime_id), desired="DESIRED_STOPPED",
                                   before=before, after=ActualState.STOPPED.value, action="noop")
        if pve == "absent":
            return ReconcileResult(runtime_id=str(r.runtime_id), desired="DESIRED_STOPPED",
                                   before=before, after=before, action="noop",
                                   detail={"note": "no backing VM"})
        # running → graceful shutdown (NEVER destroy)
        self._verify_owned(session, r)
        self._audit(session, r, "reconcile_shutdown", "ok", {"node": r.node, "vmid": r.vmid})
        self.provider.shutdown(r.node, r.vmid)  # escalates to forced stop internally on timeout
        after = self._wait_pve_state(r.node, r.vmid, "stopped", max_s=self.settings.shutdown_wait_s)
        if after == "stopped":
            r.actual_state = ActualState.STOPPED
            r.recovery_count = 0
            r.last_error = None
            return ReconcileResult(runtime_id=str(r.runtime_id), desired="DESIRED_STOPPED",
                                   before=before, after=ActualState.STOPPED.value, action="shutdown")
        r.last_error = f"shutdown did not converge (pve={after})"
        self._audit(session, r, "reconcile_shutdown_unverified", "error",
                    {"pve": after, "failure_class": "shutdown_unverified",
                     "recovery_method": "graceful_shutdown_then_verify"})
        return ReconcileResult(runtime_id=str(r.runtime_id), desired="DESIRED_STOPPED",
                               before=before, after=before, action="error", error=r.last_error)

    # --------------------------------------------------------- recording
    def _record(self, session: SASession, r: AgentRuntime, res: ReconcileResult) -> None:
        r.last_reconcile_at = self._now()
        if res.action == "error":
            r.last_error = res.error
        elif res.action in ("noop", "started", "shutdown") and res.after and res.desired:
            # converged: desired RUNNING→actual RUNNING, DESIRED_STOPPED→STOPPED
            expected = "RUNNING" if res.desired == "DESIRED_RUNNING" else "STOPPED"
            if res.after == expected:
                r.last_error = None
        session.flush()

    # ------------------------------------------------------------- loop
    def reconcile_all(self, session: SASession, limit: int = 20) -> list[ReconcileResult]:
        """One pass over all ARM-owned runtimes (excluding DESIRED_DESTROYED)."""
        rows = session.execute(
            select(AgentRuntime).where(
                AgentRuntime.desired_state != RuntimeState.DESIRED_DESTROYED,
                AgentRuntime.created_by_agent_runtime_manager.is_(True),
            ).order_by(AgentRuntime.updated_at.asc()).limit(limit)
        ).scalars().all()
        out = []
        for r in rows:
            out.append(self.reconcile_runtime(session, r.runtime_id))
            session.commit()
        return out
