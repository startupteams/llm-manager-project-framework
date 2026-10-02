"""STEA-004 §5: Server Manager (ARM) placement policy — code, not convention.

Hard filters + preference scoring, executed BEFORE any clone. The policy:
- General AI workers prefer MIAM00100.
- Inference nodes (MIAM00111/00112/00143/00144): max 1 worker per node.
- MIAM00133/MIAM00147 excluded (critical services / backup host).
- Node must be online; storage/RAM/CPU gates stay provider-backed checks.
- Human Proxmox migrations remain valid; ARM reconciles node/VM/IP afterward
  (never moves VMs itself — §5 stop condition).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from server_manager.agent_runtime_manager.models.orm import ActualState, AgentRuntime
from server_manager.common.config.settings import get_settings

log = logging.getLogger("arm.placement")

# ---- Policy constants (Jordan directive 2026-10-01, STEA-004 plan §5) ----
GENERAL_PREFERRED_NODE = "miam-00100"
INFERENCE_NODES = frozenset({"miam00111", "miam00112", "miam00143", "miam00144"})
EXCLUDED_NODES = frozenset({"miam-00133", "miam-00147"})
MAX_WORKERS_PER_INFERENCE_NODE = 1

# Worker classes that count against the inference-node cap
INFERENCE_RUNTIME_CLASSES = frozenset({"inference", "inference_worker", "gpu"})


@dataclass
class PlacementDecision:
    node: str
    score: float
    reasons: list[str] = field(default_factory=list)
    rejected: bool = False
    rejection_reason: str | None = None


def _worker_counts_by_node(session: SASession) -> dict[str, int]:
    """Live ARM runtimes per node (desired/actual not ABSENT)."""
    rows = session.execute(
        select(AgentRuntime.node, AgentRuntime.runtime_id).where(
            AgentRuntime.node.isnot(None),
            AgentRuntime.actual_state != ActualState.ABSENT,
        )
    ).all()
    counts: dict[str, int] = {}
    for node, _rid in rows:
        key = str(node or "")
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _inference_worker_counts_by_node(session: SASession) -> dict[str, int]:
    rows = session.execute(
        select(AgentRuntime.node, AgentRuntime.runtime_class).where(
            AgentRuntime.node.isnot(None),
            AgentRuntime.actual_state != ActualState.ABSENT,
        )
    ).all()
    counts: dict[str, int] = {}
    for node, rclass in rows:
        if (rclass or "") in INFERENCE_RUNTIME_CLASSES:
            key = str(node or "")
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def _candidate_nodes(session: SASession, provider) -> list[str]:
    """Nodes PVE reports online, minus exclusions."""
    try:
        nodes = provider.list_nodes()  # [(name, status), ...]
    except Exception as e:  # pragma: no cover - provider-dependent
        log.warning("node listing failed: %s", e)
        return []
    return [n for n, status in nodes if status == "online" and n not in EXCLUDED_NODES]


def select_node(
    session,  # SASession (duck-typed in tests)
    provider,
    *,
    runtime_class: str = "software_development_worker",
    template_node: str = "miam00111",
) -> PlacementDecision:
    """Choose the target node for a NEW worker VM. Fail-closed.

    Returns PlacementDecision; rejected=True when no node satisfies hard
    filters (caller must NOT clone).
    """
    s = get_settings()
    total_workers = _worker_counts_by_node(session)
    inference_workers = _inference_worker_counts_by_node(session)

    # plan §26 W4 live-found: the golden template's root disk is node-local
    # (testthin on miam00111) and cross-node clone 500s when that storage is
    # inactive on the placed node. Sandboxes (non-critical dev VMs) pin to the
    # TEMPLATE node — honest, safe, reversible; revisit when testthin is
    # active cluster-wide or the template is replicated.
    if runtime_class == "sandbox":
        return PlacementDecision(node=template_node, score=100.0,
                                 reasons=["sandbox_pinned_to_template_node"])

    scored: list[PlacementDecision] = []
    for node in _candidate_nodes(session, provider):
        reasons: list[str] = []
        penalty = 0.0

        if node == GENERAL_PREFERRED_NODE:
            scored.append(PlacementDecision(node=node, score=100.0,
                                            reasons=["general_preferred"]))
            continue

        if node in INFERENCE_NODES:
            # hard cap for INFERENCE-class workers; general workers get a
            # large penalty instead (a GPU node hosting a dev worker is
            # allowed but discouraged)
            if runtime_class in INFERENCE_RUNTIME_CLASSES:
                if inference_workers.get(node, 0) >= MAX_WORKERS_PER_INFERENCE_NODE:
                    scored.append(PlacementDecision(node=node, score=0.0,
                                                    rejected=True,
                                                    rejection_reason="inference_node_at_worker_cap"))
                    continue
            else:
                penalty += 60.0
                reasons.append("inference_node_penalty")
            score = 40.0 - penalty
            scored.append(PlacementDecision(node=node, score=score, reasons=reasons))
            continue

        # any other healthy node (exclusions already filtered)
        load_bonus = -2.0 * total_workers.get(node, 0)
        scored.append(PlacementDecision(node=str(node), score=20.0 + load_bonus,
                                        reasons=["generic", f"existing_workers={total_workers.get(str(node), 0)}"]))

    # deterministic tie-break: template node affinity LAST (clone + offline
    # storage-migrate proven), then name
    scored.sort(key=lambda d: (-d.score, d.node != template_node, d.node))
    eligible = [d for d in scored if not d.rejected and d.node]
    if not eligible:
        return PlacementDecision(node="", score=0.0, rejected=True,
                                 rejection_reason="no_eligible_node")
    return eligible[0]