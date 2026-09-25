# Operations and Usage

## What to use LLM Manager for

Use LLM Manager to:

- see currently registered model/inference routes;
- distinguish local versus cloud/provider-backed inference;
- inspect deployment history and benchmarks;
- preserve/load prior model configurations;
- coordinate model serving and recovery states;
- monitor endpoint/host health;
- expose OpenAI-compatible model routes to agents;
- eventually report raw usage/cost to ACMS.

Do not use it as the project/task/context system of record.

## Verified current API surfaces from the 2026-09 deployment

Captured directly from `service/app/main.py` on 2026-09-25 (web shell on 127.0.0.1:8001, fronted by nginx). Grouped surface:

```text
GET  /healthz                      GET  /admin, /login, /logout (LDAP/LLDAP session)
GET  /v1/models                    (OpenAI-compatible inventory via routing layer)

GET  /api/status                   GET  /api/power, /api/facility, /api/cost
GET  /api/spend, /api/spend/summary, /api/spend/analytics
GET  /api/deployments              GET  /api/deployments/{ip}/logs
POST /api/deployments/{ip}/restart POST /api/deployments/{ip}/unit
GET  /api/hosts/{ip}/presets       POST /api/hosts/{ip}/presets
POST /api/hosts/{ip}/presets/{pid}/use|rename|delete/reorder
GET  /api/history/revisions        POST /api/history/revisions
GET  /api/history/benchmarks       POST /api/history/benchmarks
POST /api/history/revisions/{revision_id}/status
GET  /api/keys                     POST /api/keys/issue|block|delete
GET  /api/openrouter               POST /api/openrouter
GET  /api/rdma/ring                POST /api/rdma/ring
POST /api/rdma/ring/{host}/toggle  GET  /api/rdma/deployments
```

The authoritative surface is the captured source (`service/app/main.py`); do not invent endpoint paths from old notes.

## Safe model-change workflow

1. Query the current LLM Manager state.
2. Capture current model/runtime configuration and a quick benchmark.
3. Ensure a rollback revision/preset exists.
4. Put the deployment into maintenance.
5. Verify every relevant recovery trigger is suppressed.
6. Make one controlled change.
7. Validate direct backend health.
8. Validate through LLM Manager.
9. Run agent/tool smoke when relevant.
10. Record benchmark/history.
11. Return desired state to serving.

## Recovery-state warning

The deployed recovery engine has **two independent triggers** (verified against source and live behavior, corrected 2026-09-25):

1. `desired_service_state='MAINTENANCE'` gates only the HTTP-probe restart path.
2. `desired_power_state` drives the outage path: a VM whose observed power state deviates from desired fires `outage_detected → recovery_vm_start`. **`STOPPED` is NOT a safe planned-stop value** — it still counts as an outage and the engine powers the VM back on (~60 s). Only `desired_power_state='STOPPED_INTENTIONAL'` takes the leave-it-off path (see `service/app/v011_recovery.py`, `tick()`).

For planned stops: set `desired_power_state='STOPPED_INTENTIONAL'` (and service state `MAINTENANCE` where relevant), restore both afterward.

This behavior is technical debt and should be replaced by a unified maintenance transaction/state.

## How to interpret model routes

A logical model name or alias is not proof of where inference runs. Verify the backing deployment/provider. Provider-backed routes have previously rotated among external providers while retaining one logical model ID.

## Deployment history

Use history to answer:

- What model/configuration was active before this change?
- What exact runtime and artifact produced this benchmark?
- Can a known-good configuration be loaded as pending?
- Which experiments failed and why?

Failed/rejected experiments should normally remain hidden unless troubleshooting or researching prior attempts.

## Working with patched runtimes

Never upgrade a known-good patched runtime in place. Clone/isolate it into a versioned venv, container, or VM and preserve the original. Capture image digest/commit/patchset and model revision before promotion.

## Handoff expectation

After significant work, leave a Markdown handoff containing:

- before/after state;
- files/config changed;
- exact model/runtime identity;
- validation/benchmarks;
- failures and diagnosis;
- rollback procedure;
- unresolved risks and next steps.
