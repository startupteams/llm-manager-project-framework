# ADR-0011 — Server Manager modular stack + Agent Runtime Manager as peer service

Status: Accepted (human-approved direction, FINAL_DRAFT_REV4 §1/§5/§7, executed 2026-09-27)

## Context

REV4 approved a Server Manager structure (LLM Manager + Agent Runtime Manager in one
repository, split hosting deferred), Python 3.12+/FastAPI/SQLAlchemy 2.x/Alembic as the
approved stack, and separate logical databases per service (acms / llmmanager /
agent_runtime_manager) with no cross-database foreign keys.

## Decision

1. New package `server_manager/` with subpackages `common/`, `llm_manager/`, `agent_runtime_manager/`.
2. Strangler staging: existing proven LLM Manager modules (service/app/*) remain the live
   implementations; `server_manager/llm_manager/adapters.py` re-exports them lazily so new
   code imports through `server_manager.*`. Old paths are removed only after parity (§5).
3. Agent Runtime Manager is GREENFIELD on SQLAlchemy 2.x ORM + Alembic (its own database
   `agent_runtime_manager`, own credentials file, no cross-DB FKs). ACMS correlation is via
   UUIDs (acms_agent_id string column), never foreign keys.
4. Machine API v1 mounted in a NEW uvicorn service (`server-manager-api`, port 8300):
   - ARM: POST /api/v1/agent-runtimes, GET /api/v1/provisioning-jobs/{job_id},
     GET /api/v1/agent-runtimes[/{id}], POST desired-state, DELETE (ownership-gated).
   - LLM: GET /api/v1/model-routes/{route}, GET /api/v1/usage (read-only this sprint).
5. Auth: scoped bearer service identities from a 0600 tokens file (§11). TDR-0007 records
   the mTLS debt.

## Consequences

- Two DBs to operate; alembic authority is per-DB (ARM now; llmmanager keeps its own ledger
  tooling until its staged ORM migration completes — TDR-0008).
- The provisioning state machine mirrors the proven agentmanager engine but adds durable
  on-VM ownership markers (§8) that the vertical slice lacked.
- The existing agentmanager vertical slice (VM121 template path) is superseded by ARM for
  provisioning; its UI remains until parity.

## Related

- TDR-0007 (service-token → mTLS), TDR-0008 (llmmanager ORM staging), FINAL_DRAFT_REV4.
