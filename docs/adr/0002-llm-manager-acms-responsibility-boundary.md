# ADR-0002: Keep Inference Infrastructure in LLM Manager and Work Orchestration in ACMS

**Status:** Accepted  
**Date:** 2026-09-25

## Context

ACMS is being designed as the higher-level orchestration and management system for persistent AI agents. A durable boundary is needed so ACMS does not also become responsible for low-level model servers, GPU placement, runtime provisioning, infrastructure capacity, and raw inference accounting.

## Decision

LLM Manager remains authoritative for agent-runtime/inference infrastructure, model-serving resources, endpoint allocation, deployment history, raw inference usage/cost, and related health/capacity.

ACMS remains authoritative for persistent agent orchestration, project/work state, context governance, human interaction/approval, work quality, and requirements/future-work governance.

ACMS may send approved model/budget/capability constraints; LLM Manager determines how to satisfy them operationally.

## Consequences

- ACMS can remain infrastructure-agnostic.
- LLM Manager must develop a stable machine-readable integration boundary.
- Some identity/version metadata must be shared between services.
- Care is required not to duplicate ACMS project/context features in LLM Manager.

## References

- `docs/ACMS_INTEGRATION.md`
- `REQ-001`, `REQ-002`, `REQ-071`
