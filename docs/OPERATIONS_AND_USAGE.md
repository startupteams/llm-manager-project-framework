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

The deployed service has included:

```text
GET /healthz
GET /v1/models
GET /api/history/revisions
GET /api/history/revisions?include_failed=true
POST /api/history/revisions
GET /api/history/benchmarks
POST /api/history/benchmarks
POST /api/history/revisions/{revision_id}/status
```

Other management endpoints exist in the deployed application and must be discovered from the live application/OpenAPI/source before automation relies on them. Do not invent endpoint paths from old notes.

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

The deployed recovery engine has historically considered both service state and VM/power state. Setting only a service-level maintenance flag has not always prevented VM restart recovery. Before intentionally stopping a VM, verify both the service and power desired states used by the current recovery engine.

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
