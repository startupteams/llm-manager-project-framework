# TDR-0010 — legacy AgentManager endpoints kept alive pending operator-UI parity

## Status

Accepted (sprint execution context, 2026-09-28; human ratification on review)

## Context

The legacy AgentManager (`service/agentmanager/main_am.py`, nginx /agents/*)
still serves the human operator UI plus write endpoints (create agent, chat,
stop/start, DELETE) that are superseded by ARM (provisioning/lifecycle) and
ACMS (semantic work, runtime controls, A2A). The parity matrix
(docs/legacy-agentmanager-parity-matrix.md) documents per-endpoint
replacements. Removing endpoints before Jordan confirms UI cutover risks
breaking his operator workflow.

## Decision

- Keep the legacy app RUNNING and read-only-safe this sprint; no removals.
- Route ALL new provisioning/lifecycle through ARM/ACMS (already enforced by
  architecture: ACMS provisions via SM API only).
- Follow-up PR will add deprecation headers + refuse NEW legacy-side
  destructive/create actions (410/409 with pointers) — deliberately NOT done
  in this flight to avoid changing operator-facing behavior unannounced.
- agentmanager schema tables: frozen (no new functionality); deletion only via
  a future destructive migration with explicit sign-off + backup.

## Repayment trigger

Jordan confirms ACMS /ui parity → legacy nginx routes retired; then the
frozen-table cleanup migration can be drafted (maintenance-window item).
