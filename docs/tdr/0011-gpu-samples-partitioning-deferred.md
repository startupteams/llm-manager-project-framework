# TDR-0009 — gpu_samples partitioning deferred

**Status:** Accepted (human-authorized deferral, 2026-09-29 execution plan §1.3)
**Related:** `db/migrations/002_gpu_samples_hourly_retention.sql`, `service/app/gpu_retention.py`

## Decision

PostgreSQL native partitioning of `gpu_samples` (by month on `ts`) is **deferred**. Retention is
implemented instead as Option A: 90-day raw retention + 2-year `gpu_samples_hourly` rollup.

## Why deferred

- Measured (2026-09-29): 1,597,825 rows / 198 MB total (~197 MB heap+index), ~82k rows/day,
  ~5-6 MB/day growth. Post-prune steady state: ~7.4M rows / <1 GB raw at any time.
- Current query patterns (latest-24-per-host, 5-minute health count, hourly averages) are
  all satisfiable by the rollup table after raw pruning — no query needs deep raw history.
- Partitioning adds operational surface (per-partition maintenance, routing, vacuum behavior)
  with no current benefit.

## Trigger conditions for revisiting (create a new ADR if ANY becomes true)

1. Raw or rollup table growth materially accelerates (>2× current MB/day with the same schema).
2. Retention DELETE batches take >5 minutes or cause meaningful autovacuum pressure at 03:00+.
3. Query latency on rollup/raw crosses 2 s p95 for dashboard endpoints.
4. Ingest rate exceeds what the batch-delete window can sustainably prune.

## Consequences

- `gpu_samples` stays a plain table with a B-tree on `sample_id` (PK) — deletes by `ts` use the
  PK-in-subquery batching in `gpu_retention.py` to keep transactions bounded.
- When partitioning happens, `gpu_samples_hourly` is unaffected (small, PK-partitionable later).
