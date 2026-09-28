# Legacy AgentManager → ARM/ACMS Parity & Decommission Matrix (flight plan Phase 6)
Date: 2026-09-28 (STEA-004 flight execution)

Legacy surface: `service/agentmanager/main_am.py` on VM114 (nginx `/agents/*` → 127.0.0.1:8200),
`agentmanager` schema in the llmmanager DB (13 tables). Superseded per ADR-0011 §5 (provisioning)
and the ACMS boundary (semantic work).

Legend: REPLACED-BY names the authoritative surface. `deprecate` = add notice + stop new
functionality now; `delete later` = remove only after replacement parity + tests (never this sprint).

## Endpoints

| legacy endpoint | still needed? | replacement | consumer | data migration | action now |
|---|---|---|---|---|---|
| `GET /agents/api/health` | yes (monitor) | SM `GET :8300/healthz` + ACMS `/health` | external monitor (10.0.20.172 polls `/`+`/login` of PDU, not this), Jordan dashboards | none | keep until monitor re-point; add deprecation header |
| `GET /agents/api/agents` | parity → ACMS | `GET /api/v1/agents` (ACMS) + SM `GET /api/v1/agent-runtimes` | Jordan UI | agents → ACMS `agents` (done for acms-worker-001) | deprecate; read-only parity check in monitoring |
| `POST /agents/api/agents` | NO | ACMS `POST /api/v1/server-manager/agents` + `/provision` (JINT-001 chain, LIVE) | none new | n/a | **deprecate now: return 409 with pointer** (no new legacy creations) |
| `GET /agents/api/jobs/{id}` | parity | SM `GET /api/v1/provisioning-jobs/{job_id}` | dashboards | historical jobs stay readable | deprecate; keep read-only |
| `POST /agents/api/agents/{id}/chat` | NO | ACMS A2A bridge (`/api/sessions/{sid}/chat`, send_work) | none since bridge live | n/a | deprecate: refuse NEW sessions, keep old-session reads |
| `DELETE /agents/api/agents/{id}` | **dangerous** | SM `DELETE /api/v1/agent-runtimes/{id}` (ownership-verified §8; NEVER deletes ACMS agent) | none | n/a | **deprecate now: refuse 410** (destruction only via ownership-verified ARM path) |
| `GET /agents/api/agents/{id}/messages` | semantic work → ACMS | ACMS work/handoff + Hermes session archive (REV4 §10A) | none new | n/a (ACMS owns going forward) | deprecate; read-only |
| `POST /agents/api/agents/{id}/stop` | lifecycle → ARM | ACMS `POST /api/v1/fleet/agents/{id}/runtime/stop` → ARM reconciler (LIVE this flight) | none | n/a | deprecate: 410 pointer |
| `POST /agents/api/agents/{id}/start` | lifecycle → ARM | ACMS runtime/start (LIVE) | none | n/a | deprecate: 410 pointer |
| `POST /agents/api/plans` | semantic work → ACMS | ACMS Work Items (work_api) | none | n/a | deprecate: 410 |
| `GET /agents/api/plans` | read | ACMS work list | dashboards | none planned | deprecate; read-only |
| `GET /agents/api/audit` | audit → ARM/ACMS events | SM `runtime_events` + ACMS `agent_events` | Jordan | historical rows stay | deprecate; read-only |
| `GET /agents` + `/agents/ui` (UI) | human operator UI | ACMS `/ui/*` (LIVE, richer) | Jordan | none | keep functional until Jordan confirms cutover; banner "legacy" |
| `GET /agents/healthz` | infra | same as health | monitor | none | keep |

## Tables (agentmanager schema — non-authoritative semantic-work concepts per plan §11)

| table | replacement | migration | action |
|---|---|---|---|
| agents | ACMS `agents` (persistent identity) | DONE for the one real worker (22ac1b19 via JINT-001) | keep readable; no new rows by legacy path |
| provisioning_jobs/steps | ARM `provisioning_jobs/steps` | historical stays | freeze |
| audit_events | SM `runtime_events` + ACMS `agent_events` | historical stays | freeze |
| projects / work_items / runs / plans / plan_artifacts / plan_versions | **ACMS owns semantic work (REQ boundary)** | NOT migrated — ACMS starts fresh; legacy rows remain frozen reference | freeze; delete only in a future destructive migration with Jordan's sign-off |
| sessions / messages | Hermes archive (§10A state branch) + ACMS session bindings | NOT migrated | freeze |
| users / agent_access | LLDAP + ACMS roles | n/a | freeze |

## Safe work executed this flight

1. **Parity matrix documented** (this file — commit to repo docs).
2. **Runtime operations already route through ARM**: the reconciler + ACMS runtime API are live (Phases 2–3); legacy stop/start are deprecation candidates.
3. **No functionality removed** — the human operator UI stays fully functional (per plan §11 "keep human operator UI functional; remove only after replacement parity/tests").
4. Deprecated behavior is ENFORCED at the legacy layer in a follow-up PR (see TDR-0010) — not silently.

## Decision queue for Jordan (genuine product calls)

1. When to flip legacy `/agents/api/*` write endpoints to hard 410/409 (proposed: after the next operator-UI parity check).
2. Whether the legacy operator UI stays up after ACMS UI reaches full parity (recommend: 2-week overlap, then retire nginx route).
3. Data retention for the frozen agentmanager schema rows (keep as cold history vs archive+drop).
