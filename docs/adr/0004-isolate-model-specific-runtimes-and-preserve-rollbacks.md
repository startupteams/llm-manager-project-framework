# ADR-0004: Isolate Model-Specific Runtimes and Preserve Known-Good Rollbacks

**Status:** Accepted  
**Date:** 2026-09-25

## Context

MARION model-serving work has required custom vLLM forks, pinned community containers, non-upstream attention/KV behavior, and model-specific patches. Upgrading a shared environment in place can destroy a configuration that is difficult to reconstruct.

## Decision

Model-specific experimental runtimes are isolated in versioned venvs, containers, or VMs. Known-good runtime environments are not upgraded in place during experiments. Before a production model is replaced, its exact configuration is preserved as a rollback revision.

## Consequences

- More disk/storage is consumed by parallel runtimes and model artifacts.
- Operations are safer and more reproducible.
- Runtime cleanup requires explicit lifecycle management.
- Model-serving history must capture runtime/image/commit/patch metadata.

## References

- `REQ-060`, `REQ-063`
- `docs/tdr/0001-patched-model-runtime-dependency.md`
