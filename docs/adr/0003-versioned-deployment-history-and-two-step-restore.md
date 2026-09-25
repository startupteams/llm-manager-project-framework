# ADR-0003: Use Versioned Deployment History and Two-Step Restore

**Status:** Accepted  
**Date:** 2026-09-25

## Context

Local model experiments frequently change model artifacts, runtime forks, context limits, parallelism, and hardware assignments. A one-click destructive rollback or a benchmark detached from configuration creates unacceptable operational risk.

## Decision

Store meaningful model-serving configurations as immutable deployment revisions and link benchmarks to those revisions.

Historical recovery uses two actions:

1. `Load Configuration` loads a revision as pending with no serving impact.
2. `Restore & Serve` is a separately confirmed state-changing action.

Failed/rejected revisions are retained but may be hidden by default.

## Consequences

- Known-good states are easier to reproduce.
- Benchmark comparisons become technically meaningful.
- The UI/data model is more complex than a single “restart old config” button.
- Restore workflows require validation and audit coverage.

## References

- `REQ-030` through `REQ-034`
- `docs/MODEL_SERVING_AND_BENCHMARKING.md`
