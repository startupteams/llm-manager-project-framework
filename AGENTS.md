# AGENTS.md — LLM Manager

These instructions apply to AI agents modifying the LLM Manager repository or MARION-IA-USA LLM Manager deployment.

## 1. Read before changing anything

Read, in order:

1. `README.md`
2. `REQUIREMENTS.md`
3. `FUTURE_WORK.md`
4. `SPRINT.md`
5. `docs/ARCHITECTURE.md`
6. `docs/IMPLEMENTATION_STATUS.md`
7. `docs/OPERATIONS_AND_USAGE.md`
8. relevant ADRs/TDRs

Approved requirements define scope. Future work is not authorization to implement.

## 2. Service boundary

LLM Manager owns inference/runtime infrastructure concerns. ACMS owns work/project/context orchestration.

Do not move project requirements, Kanban/task state, persistent transcript history, human steering governance, or general ACMS context management into LLM Manager merely because an integration needs them.

## 3. Production safety rules

- Treat MARION-IA-USA as production infrastructure.
- Never replace a known-working model/runtime before capturing its exact rollback configuration.
- Put deployments into explicit maintenance before authorized manual work.
- Recovery has historically had more than one trigger; verify both desired service state and desired power/VM state before stopping a VM.
- Do not assume a stopped model stays stopped until recovery state is verified.
- Never start two VMs that share the same passthrough GPU devices.
- Avoid repeated rapid GPU-VM stop/start cycles on MIAM-00111; a known PCIe/AER/riser condition has previously required a host reboot to recover.
- Preserve custom/patched vLLM environments. Do not `pip install -U vllm` into a known-good runtime.
- Experimental model stacks belong in versioned paths/containers/VMs.

## 4. Configuration and model truth

The live registry and verified runtime state are authoritative for current deployments.

Do not infer that an alias is local merely from its name. Verify provider/runtime/model identity.

When documenting a model deployment, capture:

- actual artifact/revision/checksum;
- runtime/image/commit;
- quantization;
- host/VM/site;
- context;
- TP/PP/DP/EP and relevant runtime flags;
- RAM/VRAM/hardware;
- route/alias;
- health and benchmark evidence.

## 5. History and rollback

Deployment history must make engineering work reproducible.

- `Load Configuration` is non-destructive.
- `Restore & Serve` is a separate confirmed operation.
- Failed experiments may be retained but hidden by default.
- Do not delete the last known-good revision merely because a newer model works.

## 6. Benchmark discipline

A performance number without deployment context is misleading.

Link benchmarks to exact deployment revisions and record at least:

- TTFT;
- output tokens/sec per request/agent;
- aggregate output tokens/sec;
- concurrency;
- prompt/output token counts;
- tested context;
- RAM/VRAM;
- host/hardware/runtime;
- errors;
- representative Hermes/tool-use status when relevant.

When comparing models, prefer same-node tests. When comparing nodes, use the same model/configuration first so hardware effects are visible.

## 7. Secrets and data

Never commit:

- `.env` files containing secrets;
- API keys/tokens;
- passwords;
- SSH/private keys;
- production database row dumps;
- customer-sensitive data;
- raw logs containing credentials.

Sanitize configuration into `.example` files. Run a secret scan before commit. If a secret appears in a tool transcript or repository candidate, stop and surface it for rotation/removal.

## 8. Git workflow

- Work on a dedicated branch.
- Branch format: `type/ticket-short-description`.
- Commit format: `type(scope): short description (#issue-id)`.
- Do not invent an issue ID; create/use an approved issue according to repository policy.
- Keep capture/bootstrapping changes reviewable.
- A human must approve before merge.

## 9. ADR policy

If a human explicitly made a significant architecture decision, record it as an ADR and surface it in the PR.

If the agent thinks a significant new decision is required, draft `Status: Proposed` and stop for human approval before treating it as accepted.

## 10. TDR policy

Use a TDR for intentional shortcuts, patched runtimes, manual recovery procedures, missing automation, known hardware/software limitations, or other accepted debt.

## 11. Validation

Run the exact project validation commands once captured/documented. At minimum for a Python service, consider syntax/import checks, tests, config parsing, migration/schema verification, and API smoke tests in a safe environment.

Report `Pass`, `Fail`, or `Not run` accurately. Never infer a passing test.

## 12. Stop and ask for human judgment when

- requirements conflict;
- an ACMS-facing contract would be invented or materially changed;
- a destructive production action is required outside the approved maintenance scope;
- credentials/access are required and unavailable;
- a security boundary changes;
- production tests fail unexpectedly;
- a new persistent model policy would be made without approved authority;
- the diff expands beyond the approved capture/documentation scope.

## 13. Handoff standard

At completion provide:

1. requirements addressed;
2. source/code captured and source locations;
3. files excluded and why;
4. changes made;
5. validation commands/results;
6. production impact (if any);
7. ADR/TDR status;
8. future-work items discovered;
9. known risks/unresolved questions;
10. exact branch, commit, PR, and recommended next action.
