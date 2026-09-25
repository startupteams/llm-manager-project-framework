# LLM Manager Requirements

This file is the source of truth for **approved LLM Manager product scope**. Acceptance criteria live with the requirement. Implementation status is documented separately in `docs/IMPLEMENTATION_STATUS.md` so approved scope is not confused with the current deployment state.

## Rules

- Stable IDs use `REQ-###`.
- Requirements are grouped by feature.
- Requirements describe one capability or constraint each.
- Unapproved ideas belong in `FUTURE_WORK.md`.
- Material requirement changes require human approval.
- Executable verification should be linked when test paths exist.

---

## Feature: Service Boundary and Business Purpose

### REQ-001 - Own inference infrastructure management

**Requirement**

LLM Manager shall be the authoritative Startup Teams service for managing or coordinating AI-agent runtime instantiation, model-serving infrastructure, inference endpoints, and inference-resource allocation.

**Acceptance criteria**

- Local and external inference resources can be represented without requiring ACMS to manage low-level model-serving processes.
- Runtime/model infrastructure decisions can be queried through LLM Manager.
- The service boundary is documented and visible to maintainers and AI agents.

**Verification**

- Architecture review against `docs/ARCHITECTURE.md` and `docs/ACMS_INTEGRATION.md`.

### REQ-002 - Preserve the ACMS responsibility boundary

**Requirement**

LLM Manager shall not become the system of record for project work, requirements, task/Kanban state, human steering history, agent transcript retention, or ACMS context governance.

**Acceptance criteria**

- ACMS-owned responsibilities are not duplicated as authoritative LLM Manager subsystems.
- Integration features are implemented as explicit boundaries rather than competing project-management features.

**Verification**

- Architecture and API review.

### REQ-003 - Support multiple execution locations

**Requirement**

LLM Manager shall represent execution location explicitly and shall not encode MARION-IA-USA as a permanent protocol assumption.

**Acceptance criteria**

- Host/runtime records include explicit site or execution-location identity.
- APIs can represent future remote, cloud, customer-owned, or additional Startup Teams locations.

**Verification**

- Data model/API review.

---

## Feature: Runtime Registration and Control

### REQ-010 - Register runtime instances

**Requirement**

LLM Manager shall maintain enough information about an instantiated agent/runtime for higher-level systems to discover and manage its inference relationship.

**Acceptance criteria**

- A runtime record can identify the runtime instance, human-readable name, harness type/version, execution host/location, current inference assignment, health, and LLM Manager configuration version where available.
- Missing capabilities are represented explicitly rather than guessed.

**Verification**

- Runtime registry API/data-model test.

### REQ-011 - Expose runtime capability discovery

**Requirement**

LLM Manager shall expose machine-readable capability information for runtime controls that may include start, stop, restart, pause, resume, interrupt, steering, model changes, health reporting, usage reporting, and cost reporting.

**Acceptance criteria**

- Unsupported operations are reported as unsupported.
- Capability discovery does not simulate behavior the underlying runtime cannot perform.

**Verification**

- Capability-manifest tests once the interface is approved.

### REQ-012 - Coordinate runtime lifecycle control

**Requirement**

When supported by the underlying runtime and authorized, LLM Manager shall facilitate start, stop, restart, pause, resume, interrupt, and steering operations for registered runtimes.

**Acceptance criteria**

- Control operations are authenticated/authorized.
- Current state and result are observable.
- Unsupported controls fail clearly.

**Verification**

- Runtime-adapter integration tests.

### REQ-013 - Version material runtime configuration

**Requirement**

LLM Manager shall make material runtime/inference configuration versions visible so a higher-level system can distinguish a persistent agent identity from a particular runtime/model configuration.

**Acceptance criteria**

- Model assignment and other LLM Manager-owned behavior-changing configuration can be associated with a version/revision.
- Restarting a process does not implicitly create a new logical agent identity.

**Verification**

- Deployment-revision and runtime-registration tests.

### REQ-014 - Require approval for material persistent model-policy changes

**Requirement**

Persistent model/inference policy changes that materially alter expected behavior or cost shall be human-approved or traceably derived from an approved policy.

**Acceptance criteria**

- Autonomous subordinate agents cannot silently persist a new model policy.
- The approved change is auditable in configuration history.

**Verification**

- Authorization/audit tests.

---

## Feature: Inference Registry, Routing, and Capacity

### REQ-020 - Register local and cloud inference endpoints

**Requirement**

LLM Manager shall maintain a registry of local and external inference endpoints with explicit model identity, provider/runtime classification, execution location, health, and relevant context/capacity limits.

**Acceptance criteria**

- Local versus cloud/provider-backed endpoints are distinguishable.
- Model IDs do not falsely imply a local deployment.
- Endpoint health can be queried.

**Verification**

- Registry and `/v1/models` integration tests.

### REQ-021 - Report health and capacity

**Requirement**

LLM Manager shall report endpoint and inference-host health plus capacity/load indicators useful for operational decisions.

**Acceptance criteria**

- Unhealthy/degraded state is distinct from healthy serving state.
- Capacity information can include concurrency/load/context headroom where the runtime exposes it.

**Verification**

- Health collector tests.

### REQ-022 - Apply approved inference constraints

**Requirement**

LLM Manager shall be able to select or allocate inference resources subject to approved constraints such as maximum cost, local preference, cloud permission, capability class, or explicit model choice.

**Acceptance criteria**

- Policy constraints are represented separately from low-level implementation details.
- If constraints cannot be satisfied, the service reports the failure rather than silently violating policy.

**Verification**

- Policy/allocation tests after the policy schema is approved.

### REQ-023 - Preserve explicit model identity

**Requirement**

LLM Manager shall record and expose the actual served model artifact/revision rather than relabeling a different model to match a desired logical name.

**Acceptance criteria**

- Model artifact/revision is traceable for local deployments.
- Provider-backed model routes are identified as provider-backed.

**Verification**

- Deployment-history and provider-routing tests.

### REQ-024 - Support stable logical routes without hiding backend identity

**Requirement**

LLM Manager may provide stable logical model routes/aliases, but the backing model/provider shall remain inspectable.

**Acceptance criteria**

- A user/agent can determine the backing deployment for an alias.
- Alias changes are auditable.

**Verification**

- Routing tests.

### REQ-025 - Support maintenance suppression

**Requirement**

LLM Manager shall provide an intentional maintenance state that prevents automated recovery from interfering with authorized manual maintenance.

**Acceptance criteria**

- Both service recovery and VM/power recovery triggers are suppressed when maintenance requires them to be suppressed.
- The UI/API makes the effective maintenance state clear.

**Verification**

- Recovery-engine integration test covering service state and power state.

### REQ-026 - Coordinate VM/power state safely

**Requirement**

LLM Manager shall distinguish desired service state from desired VM/power state and shall not restart a VM that an authorized maintenance operation intentionally stopped.

**Acceptance criteria**

- Desired state transitions are audited.
- Recovery does not create restart loops during maintenance.

**Verification**

- Recovery tests.

---

## Feature: Deployment History and Rollback

### REQ-030 - Store immutable deployment revisions

**Requirement**

LLM Manager shall store reproducible deployment revisions for meaningful model-serving configurations.

**Acceptance criteria**

- A revision can include model artifact/revision/checksum, runtime version, host/VM, hardware, context, parallelism, relevant environment/launch data, service unit, aliases, and status.
- Historical revisions remain available after the active deployment changes.

**Verification**

- Deployment-history API/database tests.

### REQ-031 - Link benchmarks to exact deployment revisions

**Requirement**

Every stored benchmark shall identify the exact deployment revision and hardware/runtime configuration that produced it.

**Acceptance criteria**

- Benchmark data is not represented as a context-free model score.
- A benchmark can be traced back to model/runtime/host configuration.

**Verification**

- Benchmark-history relational-integrity tests.

### REQ-032 - Load a historical configuration non-destructively

**Requirement**

A user shall be able to load a historical deployment revision into a pending configuration without stopping or modifying the active deployment.

**Acceptance criteria**

- `Load Configuration` makes no serving-state change.
- The pending diff is reviewable before activation.

**Verification**

- UI/API test.

### REQ-033 - Restore and serve through a separate confirmed action

**Requirement**

Activating a historical configuration shall require a separate `Restore & Serve` action with explicit confirmation.

**Acceptance criteria**

- The action shows the current deployment and the selected target revision.
- A failed restore preserves or returns to the prior working deployment when safe.
- The action is audited.

**Verification**

- Restore workflow integration test.

### REQ-034 - Retain failed experiments without cluttering default views

**Requirement**

LLM Manager shall retain failed/rejected deployment experiments as engineering history while allowing them to be hidden by default.

**Acceptance criteria**

- Failed revisions have explicit status and failure reason.
- The default UI can exclude failed/rejected rows.
- A user can opt in to view them.

**Verification**

- History filter tests.

---

## Feature: Benchmarking and Model Qualification

### REQ-040 - Capture inference performance metrics

**Requirement**

LLM Manager shall store performance measurements sufficient to compare deployments operationally.

**Acceptance criteria**

- Stored metrics can include TTFT, per-request/per-agent output tokens/sec, aggregate output tokens/sec, concurrency, latency, prompt/output tokens, success/error counts, and tested context.
- RAM/VRAM and relevant hardware/runtime metadata are stored when available.

**Verification**

- Benchmark API/data tests.

### REQ-041 - Support long-context qualification

**Requirement**

LLM Manager shall support recording explicit long-context acceptance tests for deployments intended for long-running agents.

**Acceptance criteria**

- Configured context and actually tested context are distinct fields.
- Long-context failure is visible in deployment history.

**Verification**

- Qualification test record.

### REQ-042 - Record agent-runtime compatibility checks

**Requirement**

LLM Manager shall allow deployment qualification to record whether representative agent/runtime operations such as tool calling and long-session interaction succeed.

**Acceptance criteria**

- Hermes/tool-smoke status can be stored for a deployment/benchmark.
- Failure is not hidden by a healthy backend HTTP status.

**Verification**

- Runtime smoke test and benchmark record.

### REQ-043 - Capture hardware fingerprints with comparative benchmarks

**Requirement**

Comparative benchmarks shall record enough hardware information to distinguish model effects from host differences.

**Acceptance criteria**

- Host/VM, CPU, RAM capacity/speed when available, GPU type/count, and relevant PCIe/topology data can be associated with the result.

**Verification**

- Benchmark metadata review.

---

## Feature: Usage and Cost

### REQ-050 - Report raw inference usage

**Requirement**

LLM Manager shall collect/report raw inference usage measurements where available, including token counts, request counts, duration, and context utilization.

**Acceptance criteria**

- Usage records include timestamps and model/provider identity.
- Missing provider metrics are represented as unavailable rather than fabricated.

**Verification**

- Usage collector tests.

### REQ-051 - Calculate local inference operating cost

**Requirement**

LLM Manager shall support local inference cost reporting using authoritative electrical/power measurements from PDU Manager where available.

**Acceptance criteria**

- Energy measurements retain source and freshness.
- Estimated allocation is marked as estimated.

**Verification**

- PDU/cost integration tests once implemented.

### REQ-052 - Report cloud/provider inference cost

**Requirement**

For external providers, LLM Manager shall report available monetary cost/usage derived from provider usage/pricing data.

**Acceptance criteria**

- Provider/model/time period are identifiable.
- Estimated versus measured/charged values are distinguishable.

**Verification**

- Provider cost collector tests.

### REQ-053 - Expose cost information to ACMS

**Requirement**

LLM Manager shall eventually expose machine-readable current cost rate, selected-period cost, model/provider cost, attributable local energy cost, external-provider spend, freshness, and estimation confidence for ACMS consumption.

**Acceptance criteria**

- ACMS does not need to recalculate low-level inference cost independently.
- Month-end projection can be added without changing the raw usage contract.

**Verification**

- ACMS integration contract test after interface approval.

---

## Feature: Safety, Audit, and Operations

### REQ-060 - Preserve working state before destructive model changes

**Requirement**

Before replacing a known-working model/runtime configuration, LLM Manager operations shall preserve enough configuration and validation evidence to restore that state.

**Acceptance criteria**

- Exact launch/runtime information is captured before replacement.
- A rollback revision exists for production changes.

**Verification**

- Operational checklist / deployment workflow test.

### REQ-061 - Audit significant control actions

**Requirement**

Significant model, deployment, desired-state, recovery, and restore actions shall be auditable.

**Acceptance criteria**

- Actor, timestamp, intent/action, target, and result are retained where practical.

**Verification**

- Audit-log tests.

### REQ-062 - Never commit or expose infrastructure secrets

**Requirement**

LLM Manager source, history, logs intended for source control, and generated handoffs shall not contain API keys, private keys, passwords, tokens, or secret-bearing `.env` contents.

**Acceptance criteria**

- Repository secret scanning is part of validation.
- Example configuration uses placeholders.

**Verification**

- Secret-scan command / review.

### REQ-063 - Preserve reproducibility for patched model runtimes

**Requirement**

When a local model requires a patched/custom runtime, LLM Manager documentation/history shall capture enough version and patch metadata to recreate it without mutating unrelated known-good runtimes.

**Acceptance criteria**

- Custom runtime path/image digest/commit is stored.
- Experimental runtime is isolated from known-good runtime.

**Verification**

- Deployment revision and operations review.

---

## Feature: API and User Interface

### REQ-070 - Expose service health and model inventory

**Requirement**

LLM Manager shall expose machine-readable service health and current model inventory through stable APIs.

**Acceptance criteria**

- Service health can be queried without relying on the dashboard.
- Model inventory includes explicit IDs and current availability.

**Verification**

- `/healthz` and model-list integration tests.

### REQ-071 - Provide ACMS-readable management data

**Requirement**

LLM Manager shall provide a machine-readable interface for runtime identity/state, inference assignment, usage, cost, host/endpoint health, and capacity needed by ACMS.

**Acceptance criteria**

- The interface can evolve without requiring ACMS to know vLLM/Proxmox internals.
- Missing or unsupported fields are explicit.

**Verification**

- Future ACMS contract tests.

### REQ-072 - Use explicit authorization for control APIs

**Requirement**

State-changing management APIs shall require authenticated authorization appropriate to the action risk.

**Acceptance criteria**

- Read-only and mutating operations can be distinguished.
- Unauthorized control attempts fail without side effects.

**Verification**

- Authorization tests.

### REQ-073 - Show deployment history and benchmark context in the UI

**Requirement**

The dashboard shall make deployment history and benchmark results inspectable without hiding the hardware/runtime context that produced them.

**Acceptance criteria**

- Successful history is visible by default.
- Failed/rejected history can be shown.
- Benchmark rows identify deployment revision and key performance/configuration fields.

**Verification**

- Dashboard/UI tests.
