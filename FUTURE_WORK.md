# LLM Manager Future Work

This file contains **unapproved or deferred scope**. Items must not be silently implemented merely because they are recorded here. Human approval promotes an item into `REQUIREMENTS.md` and an implementation sprint.

## ACMS integration

### FW-001 - Approve the ACMS <-> LLM Manager API contract

Define protocol, authentication, request/response schemas, versioning, and error semantics for runtime registration, runtime state, inference state, usage, cost, health, capacity, and control requests.

### FW-002 - Canonical ACMS agent ID mapping

Define the persistent mapping among ACMS agent identity, LLM Manager runtime identity, harness/runtime process identity, and transient sessions.

### FW-003 - Configuration locking/version semantics

Define how known-good agent/runtime configuration versions can be frozen/locked, unlocked, compared, and updated without conflating configuration with work/context changes.

### FW-004 - Runtime adapter capability manifest

Define and implement a stable capability manifest and adapters for Hermes, Codex, Pi, and future harnesses.

### FW-005 - Cross-site runtime discovery and routing

Generalize discovery/allocation beyond MARION-IA-USA to remote sites, cloud runtimes, customer-owned infrastructure, and future Startup Teams locations.

## Routing and capacity

### FW-010 - Sticky-session routing

Add session affinity so repeated requests from the same agent tend to reach the same local model replica and benefit from prefix/cache locality.

### FW-011 - Per-node and global concurrency limits

Add explicit per-deployment concurrency caps, queueing/backpressure, and load-aware placement for large agent fleets.

### FW-012 - Policy-driven inference allocator

Implement approved constraints such as cost ceilings, local preference, cloud permission, capability class, context requirement, and explicit model selection.

### FW-013 - Automatic benchmark after successful deployment

Run a bounded quick benchmark automatically after a deployment becomes healthy, while allowing maintenance/tests to disable the automatic run.

### FW-014 - Automated model qualification profiles

Standardize reusable C1/C4/C8/long-context/tool-use qualification profiles and maintain versioned benchmark definitions.

## Cost and usage

### FW-020 - PDU Manager energy integration

Consume authoritative electrical measurements and define a defensible method for attributing shared-host energy to inference workloads.

### FW-021 - Cloud provider usage/cost collectors

Collect usage and monetary cost consistently across external providers while preserving provider-specific raw data.

### FW-022 - Month-end cost projection

Define the Gregorian-month projection algorithm, confidence status, and treatment of irregular workloads.

### FW-023 - Agent/session/task cost attribution

Decide how much cost attribution belongs in LLM Manager versus ACMS and define stable attribution keys.

## History, restore, and change control

### FW-030 - Complete end-to-end Restore & Serve automation

Verify/finish the two-step historical restore workflow, including automated rollback on failed activation and complete audit coverage.

### FW-031 - Deployment comparison UI

Add multi-select comparison of revisions across context, concurrency, TTFT, per-agent throughput, aggregate throughput, RAM/VRAM, runtime, quantization, and hardware.

### FW-032 - Reproducible schema migrations

Introduce a formal migration mechanism for LLM Manager database changes rather than relying on ad hoc additive production changes.

## Reliability and recovery

### FW-040 - Unified maintenance state

Replace the operational need to coordinate `desired_service_state` and `desired_power_state` manually with one explicit maintenance transaction/state that suppresses every recovery trigger safely.

### FW-041 - GPU VM kernel/driver pinning

Standardize kernel/NVIDIA module compatibility for GPU VMs to prevent cloned/rebooted VMs from booting a kernel without matching NVIDIA modules.

### FW-042 - Normalize inference systemd units

Remove legacy naming differences such as `vllm.service` versus model-specific units and make deployment control consistent across hosts.

### FW-043 - Hardware health integration

Expose persistent hardware degradation (ECC, PCIe/AER, GPU/NIC issues) as structured host health that routing/maintenance logic can consume.

## Model-serving engineering

### FW-050 - Qwen3.8-Flash-Next production optimization

After stable promotion, evaluate MTP speculative decoding, FP8 KV cache, calibrated scales, P2P-capable driver improvements, and C8+ tuning without jeopardizing the working TP2×PP3 baseline.

### FW-051 - Model-runtime recipe catalog

Maintain approved reproducible recipes for custom/patched local models so model installation does not depend on shell history or a single engineer's knowledge.

### FW-052 - Artifact/cache lifecycle management

Define safe retention, deduplication, checksum, cleanup, and storage-pressure policies for large model artifacts and experimental runtimes.

## Security and governance

### FW-060 - Approve ACMS-issued command authorization model

Define roles, scopes, service-to-service authentication, audit requirements, and human-approval boundaries for ACMS-issued runtime/model commands.

### FW-061 - Automated secret scanning and rotation workflow

Add repository/CI secret scanning and documented key-rotation procedures for provider/API credentials.

### FW-062 - External/customer workload isolation

Before AgentifyMe/customer workloads use the same control plane, define stronger tenant/customer isolation, policy boundaries, and sensitive-information handling.
