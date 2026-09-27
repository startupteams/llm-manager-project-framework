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
