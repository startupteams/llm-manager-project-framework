# TDR-0001: Patched Model Runtime Dependency

**Status:** Accepted

## Description

Some high-value local models depend on custom or community-patched vLLM environments rather than one stock upstream runtime. Known examples include custom TURBOQUANT behavior and the PP-enabled Ampere Qwen3.8-Flash-Next stack.

## Impact

Runtime upgrades can silently remove required features, break loaders, change model fit, or make historical benchmark results non-reproducible. Multiple environments consume storage and increase maintenance complexity.

## Possible Solutions

- Maintain an approved recipe/image catalog with exact commits/digests and automated qualification.
- Upstream required fixes and migrate back to stock runtimes once equivalent support is released and qualified.

## Owner

Startup Teams LLM Manager / infrastructure maintainers.
