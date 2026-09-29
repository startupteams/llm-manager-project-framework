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
- **Slices 5–6 (planned):** 5 = write-path parity behind the adapter layer (verify by row-count +
  checksum before cutover); 6 = remaining telemetry tables AFTER the retention decision lands.
  Legacy psycopg2 paths stay until write parity is proven (TDR-0008 rule).
