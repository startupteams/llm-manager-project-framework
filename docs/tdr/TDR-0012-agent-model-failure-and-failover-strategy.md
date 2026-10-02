# TDR-0012 — Agent Model Failure and Failover Strategy (Session Model Pin, Cloud-Only Failover)

- **Status:** Accepted (conservative interim policy)
- **Date:** 2026-10-02
- **Owner:** Hermes runtime (hermes-agent, VM906 profile `agent_stea004_entrepreneur`)
- **Related:** work-down doc `2026_10_02_hermes_session_model_pinning_cloud_failover_workdown.md`

## Context

On 2026-10-02, session `20261002_111323_e0854ec2` (primary `z-ai/glm-5.3-flash`
via OpenRouter, 455 calls) emitted a malformed tool call with an **empty
function name**. OpenRouter rejected the replay with HTTP 400. Hermes
classified the failure as failover-eligible and silently activated
`fallback_providers[0]` — the **local** model `qwen3.6-35b-a3b` via LLM
Manager — with no user authorization. The compression-fork/rotation path then
persisted every continuation session with `model=agent.model` (the fallback
model), locking 13+ sessions onto Qwen. This happened twice that day
(~09:29, ~13:41).

A model switch changes reasoning behavior, tool-call behavior, context
limits, and continuity assumptions of a long-running agent session. Silent
local fallback is therefore a correctness bug, not an availability feature.

## Current accepted behavior (implemented 2026-10-02)

When `model.pin_sessions: true` (hermes-agent `agent/chat_completion_helpers.py`,
`try_activate_fallback`):

1. The configured local fallback chain is **unreachable** for automatic
   switching. Refusals are audited (`MODEL_CHANGE ... source=REFUSED_INELIGIBLE`).
2. The **only** permitted automatic transition is a **single, bounded**
   failover to the cloud route in `model.automatic_failover.cloud`:
   `deepseek-v4.1-flash-api` via LLM Manager (`https://llm-manager.marion-ia-usa.internal/v1`,
   credential referenced by env name `MARION_LOCAL_API_KEY` only — never
   logged/committed).
3. Eligible failure classes are provider-side availability failures only:
   `timeout`, `overloaded` (503/529), `server_error` (500/502), `rate_limit`.
   Everything else — notably `format_error` (malformed/empty tool-call
   names), `context_overflow`, auth, billing, model_not_found, policy
   blocks, `unknown` — **fails closed**: stop and require human action.
4. Failover is temporary: the pinned primary (`agent._primary_runtime`) is
   never overwritten; `restore_primary_runtime` returns the session to the
   primary next turn. Compression forks inherit `agent.model`, which the pin
   protects from mutation.
5. If primary AND cloud failover both fail, the agent stops work safely and
   notifies the user instead of picking another model.

This is acceptable for now. Reliability and model identity outrank
uninterrupted execution.

## Reason

See Context. Failing closed is acceptable; silently switching to an
unrelated local model is not.

## Future technical debt

Design a better long-term failover strategy, potentially including:

- multiple approved cloud fallback models / capability-compatible fallback classes
- health-aware provider routing, circuit breakers, retry budgets
- context-window and tool-call compatibility checks before failover
- cost/latency policy, per-agent and per-task fallback policy
- failover telemetry, model capability metadata
- automatic return-to-primary with resumption after outage
- operator escalation paths

## Open architecture question

Should an agent continue work on a fallback model at all, or should
fallback only preserve enough functionality to checkpoint, write a handoff,
notify the operator, and stop safely? **Unresolved — left as an explicit
question.**

## Implementation pointer

- `hermes-agent` branch `feat/session-model-pin-cloud-failover`
  (local on VM906, base `f3d2dfbec`): `agent/agent_init.py`,
  `agent/chat_completion_helpers.py`, `tests/run_agent/test_session_model_pin.py`
  (25 tests).
- Verification: `tests/run_agent` 1828 passed / 3 skipped (0 failures) on
  both clean and modified trees; `tests/agent`+`tests/gateway` wider sweep
  has 2868 **pre-existing** failures, byte-identical failure sets on clean
  and modified trees (environment-dependent, unrelated to this change).