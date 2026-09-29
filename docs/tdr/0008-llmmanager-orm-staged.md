# TDR-0008 — llmmanager DB ORM migration is staged, not complete

Status: Accepted (REV4 §6 allows staged/behavior-preserving migration)

## Context

The llmmanager database has 20+ public tables + an agentmanager schema, all created by raw
SQL (db/schema/schema.sql) and accessed by psycopg2 in the 2,600-line single-file app.
Migrating all of it to SQLAlchemy ORM in one sprint risks regressions in the revenue-critical
LLM serving path.

## Decision

- This sprint migrates ONLY the new agent_runtime_manager database to ORM+Alembic (greenfield).
- LLM Manager machine API reads (model-routes, usage) go through SQLAlchemy Core `text()`
  queries against the EXISTING schema — no schema change, no ORM models for those tables yet.
- Existing write paths stay on the proven psycopg2 app code.
- Follow-up sprints migrate tables group by group (deployments/hosts/model_registry first,
  telemetry second) behind the adapter layer, each with row-count + checksum verification per REV4 §6.

## Consequences

- Two persistence styles coexist temporarily in the llmmanager DB (raw SQL + Core reads).
- `server_manager.llm_manager.adapters` isolates the eventual cutover.

## Slice status log (2026-09-29)

- **Slice 1 (merged #47):** hosts / active_hosts / model_registry reflective ORM.
- **Slices 2–3 (merged #49):** deployment_revisions / deployments + benchmark_runs /
  qualification_envelopes reflective ORM + 4 read endpoints.
- **Slice 4 (this phase, read-only inventory + scoped models):**
  - **gpu_samples retention dependency:** gpu_samples is 197 MB / 1,591,369 rows of a 225 MB DB
    (re-measured 2026-09-29; prior proposal said 191 MB / 1.54M — growth ~ +5 MB/week at the same
    order). Retention decision STILL PENDING (human). Any ORM work that would WRITE to gpu_samples
    or change its schema stays blocked; the retention proposal remains
    GPU_SAMPLES_RETENTION_PROPOSAL (rollup table + N-day raw window; nothing deleted without
    approval).
  - **NOT blocked by retention:** electricity_rates, emporia_devices, emporia_channel_map,
    facility_power_samples, host_power_rollup_1m, facility_power_rollup_1d, recovery_events,
    request_routing_log, manager_settings, rdma_ring_nodes, multi_node_deployments — read-only
    ORM parity for these is safe and additive.
- **Slice 5 (merged, 2026-09-29):** write-path parity + CUTOVER plumbing.
  - `service/app/orm_write_adapter.py` — sync SQLAlchemy 2 Core adapter over the SAME schema (no DDL;
    column-whitelist guard; same psycopg2 credential contract; socket-dir URL trap handled).
  - Parity tests `tests/test_orm_slice5_cutover.py` (7): hosts update/inventory/flags, model_registry
    upsert/unroutable/alias flow, recovery_events insert + recent_count, checksum parity probe.
  - **Legacy-parity finding:** the UNIQUE (logical_model_name, host_id) constraint does NOT dedupe NULL
    host_ids (aliases) — legacy delete-then-insert order is required; the adapter mirrors it exactly.
  - v011_core desired-state/power-off/sync_registry/routable_models + v011_recovery audit()/recent_count()
    route through the adapter behind env `ORM_WRITE_CUTOVER=1` (default OFF = legacy psycopg2; instant
    rollback = unset the env).
  - Static source test window widened (indent shift) — semantic guarantee unchanged.
- **Slice 6 (merged, 2026-09-29):** gpu_samples family ORM access (retention live → unblocked).
  - `service/app/gpu_orm.py` — sync SQLAlchemy Core adapter: BULK insert_samples_bulk (one multi-row INSERT
    per collector cycle — never row-at-a-time ORM objects), hourly_rollup_window (IDENTICAL upsert SQL to
    gpu_retention._rollup_window incl. power_sum_wh/last_ts + DO UPDATE refresh), prune_raw_older_than
    (bounded batch on sample_id PK), window reads (mappings-batched), retention_settings (config keys
    gpu_samples_{,hourly_}retention_days), parity checksums.
  - Collector `insert_samples` routes through gpu_orm behind `ORM_WRITE_CUTOVER=1` (fall-through to legacy
    executemany on adapter error — the collector must never stall).
  - Tests `tests/test_gpu_orm_slice6.py` (5, pgserver): bulk insert, hour-aligned upsert refresh (W2 lesson
    verified), bounded prune keeps aggregates, window reads, parity probe shape. **Schema truth:** PK is
    sample_id (not id); numeric columns → Decimal; UNIQUE (name, host_id) does NOT dedupe NULLs.
  - Retention guarantees preserved: 90d raw / 730d hourly, aggregate-before-prune, closed-hour alignment,
    scheduled timer — all untouched.
- **Still on legacy psycopg2 after both slices (deliberate):** LiteLLM-owned writes (never absorbed),
  gpu_retention.py operational SQL (verified machinery — cutover optional, flag-gated path exists),
  spend_guard / cost reads. Staging/prod shadow validation for the ORM_WRITE_CUTOVER=1 path: next deploy.
- **Slices 6 (planned):** telemetry tables — now unblocked by the retention decision (executed 2026-09-29).
  Legacy psycopg2 paths stay until staging + prod shadow validation completes (this sprint).
