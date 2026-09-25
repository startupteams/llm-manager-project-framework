# TDR-0003: Heterogeneous Hardware and Model Portability

**Status:** Accepted

## Description

The current local inference fleet mixes GPU generations, VRAM sizes, RAM characteristics, CPU-only/unified-memory systems, and model-specific runtime requirements. The same model configuration is not portable across every host.

## Impact

Performance and fit must be qualified per hardware class. Model selection, parallelism, context, and quantization cannot be inferred from a result on another host. Operational documentation and benchmark metadata are more complex.

## Possible Solutions

- Maintain hardware-class fingerprints and qualification profiles.
- Add policy-aware placement using measured deployment capabilities.
- Standardize future hardware purchases where business value justifies it.

## Owner

Startup Teams infrastructure / LLM Manager maintainers.
