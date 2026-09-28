# TDR-0009 — Single-replica reconciler without leader election

## Status

Accepted (sprint execution context, 2026-09-28; human ratification on review)

## Context

The ARM desired-state reconciler (§10B) executes infrastructure actions
(start/shutdown of runtime VMs). Two or more concurrent reconciler instances
could race on the same runtime (double start / stop/start oscillation).
Proper leadership (lease table, Postgres advisory locks, or external
coordinator) is not yet built.

## Decision

Ship the reconciler as single-replica-by-deployment-discipline:

- `server-manager-api` runs as a single uvicorn worker (systemd unit pins one
  process; scaling policy documented as "do not run a second replica until
  leadership exists").
- `POST /api/v1/agent-runtimes/{id}/reconcile` and `POST /api/v1/reconcile`
  are serialized by per-runtime row commits; concurrent API callers converge
  but are not coordinated.
- `reconcile_all` orders by `updated_at ASC` and commits per row.

## Consequences / risks

- If a second `server-manager-api` process is ever started, races become
  possible → mitigation: the systemd unit is the only sanctioned runner;
  a leader-election migration must land before any horizontal scale-out.
- Bounded retry/backoff state lives per-runtime (`recovery_count`,
  `last_reconcile_at`, `last_error`), so a crashed reconciler resumes safely
  on next pass (idempotent observation + convergent actions).

## Repayment trigger

Any of: multi-replica deployment, split reconciler process, or a second
infrastructure actor writing desired-state (ACMS runtime controls).
