# Implementation Status and Known Limitations

**Snapshot basis:** September 2026 handoffs and design discussions. This file is descriptive, not the live source of truth. Verify the running system before operating it.

## Implemented / observed in the current deployment

### Core service and routing

- LLM Manager is deployed on VM114 in MARION-IA-USA.
- Service health is exposed through `/healthz`.
- OpenAI-compatible model inventory/routing is exposed through `/v1/models` via the manager/routing layer.
- Local vLLM endpoints, llama.cpp endpoints, and cloud/provider-backed routes have all been managed through the service.
- A recovery/control subsystem tracks desired state and can attempt service/VM recovery.

### Deployment history and benchmarks

The September 2026 deployment added:

- `deployment_revisions` history;
- expanded `benchmark_runs` fields linked to deployment revisions;
- history/benchmark API endpoints;
- dashboard deployment-history display;
- successful-versus-failed visibility filtering;
- baseline hosting-command presets;
- non-destructive `Load Configuration`/pending-configuration behavior.

### Model-serving examples

Recent MARION work has demonstrated:

- Qwen3.8-27B on RTX 3080 hardware at 70K context using a custom vLLM fork.
- Qwen3.6-35B-A3B using DP/EP and, on some hosts, a custom TURBOQUANT path.
- Gemma 4 26B-A4B as a two-node local pool with strong C4/C8 throughput and Hermes tool-call success.
- Qwen3.8-Flash-Next successfully qualified on six RTX 3080 20 GB GPUs using a pinned PP-enabled Ampere vLLM stack, TP2×PP3+EP, FP8 PLE CPU offload, 70K context, and working vision.
- llama.cpp on MIAM-00149, with larger 27B CPU-only experiments shown to be impractically slow for the desired 20 tok/s target.

These examples are operational evidence, not permanent product requirements.

## Partially implemented / not yet proven complete

- End-to-end `Restore & Serve` automation has been designed but should be treated as incomplete until the full confirmed activation + automatic rollback workflow is tested.
- Sticky-session routing and explicit per-node concurrency caps are not part of the older v0.11 baseline.
- Runtime capability discovery across Hermes/Codex/Pi is not yet a stable generic interface.
- ACMS-facing management API/authentication contract is not approved.
- Configuration locking/frozen versions are not yet defined as a complete feature.
- Automated benchmark-after-load is not yet a complete standard workflow.
- Local/cloud cost attribution and PDU-energy integration are not yet complete.
- Cross-site routing/discovery is future work.

## Known software limitations

### Recovery state has multiple triggers

Maintenance has historically required coordinating both desired service state and desired VM/power state. A VM can be restarted by recovery if only one axis is suppressed.

### Patched model runtimes

Several useful local models depend on custom forks, version-pinned containers, or non-upstream features. Upgrading stock vLLM in place can destroy known-good behavior.

### Unit/config inconsistency

Some inference VMs have used different systemd unit names and locally evolved service files. This increases automation risk.

### Greenfield/additive schema evolution

History/benchmark features were added to a running v0.11 service. A formal migration/versioning system is still desirable.

## Known hardware/operational limitations

### Heterogeneous GPU hardware

The fleet includes different GPU generations and VRAM sizes. A configuration that works on RTX 5060 Ti/Blackwell hardware cannot be assumed to work on RTX 3080/Ampere and vice versa.

### MIAM-00111 PCIe/AER condition

Rapid GPU-VM stop/start cycles have previously contributed to a PCIe/AER/riser wedge that required a host reboot. Avoid unnecessary churn and preserve clean rollback procedures.

### MIAM-00112 ECC degradation

Corrected ECC activity on a memory channel has been treated operationally as accepted hardware degradation. It should be monitored and surfaced if it becomes uncorrectable or causes instability.

### MIAM-00149 performance envelope

The CPU/unified-memory llama.cpp host is useful for prototypes and small models, but a Qwen3.8-27B GGUF experiment was far below the desired production throughput target at long context.

## Security limitations / open work

- ACMS-issued control authorization is not yet fully designed.
- External/customer workload isolation requires additional governance before customer-sensitive workloads share the same management assumptions.
- Secret scanning and standardized credential-rotation workflows should become part of CI/operations.
