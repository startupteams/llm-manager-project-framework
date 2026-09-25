# Agent Readiness - LLM Manager Level 3 Target

LLM Manager targets production-grade agent readiness because AI agents routinely perform model/runtime/infrastructure work against a live heterogeneous environment.

## 1. Style and validation

- [ ] Formatting/lint/static-analysis commands are captured from the deployed codebase.
- [ ] Type checking is defined if practical for the captured Python code.
- [ ] Validation commands are documented in the README or developer docs.

## 2. Reproducible build/runtime

- [ ] Python/runtime versions are pinned/documented.
- [ ] Dependencies are captured without copying the production venv.
- [ ] A fresh non-production checkout can start the service using documented steps.
- [ ] Model-specific patched runtimes are documented separately from the manager application runtime.

## 3. Testing

- [ ] Unit tests cover pure management logic.
- [ ] Integration tests cover database/history/routing/control behavior using non-production fixtures.
- [ ] Recovery/maintenance tests cover both desired service and desired power state.
- [ ] History/load/restore tests exist.
- [ ] API smoke tests cover `/healthz`, model inventory, history, and benchmark surfaces.

## 4. Documentation

- [x] `AGENTS.md` exists.
- [x] Approved scope lives in `REQUIREMENTS.md`.
- [x] Future work is separated into `FUTURE_WORK.md`.
- [x] Architecture has a defined home.
- [x] Current limitations/status have a defined home.
- [ ] Exact setup/run/test/deploy commands are populated after source capture.
- [ ] Recurring failures have troubleshooting documentation.

## 5. Safe execution

- [ ] Agents can use a test/non-production path for manager code changes.
- [ ] Production model changes always preserve rollback.
- [ ] Destructive actions require explicit approved scope.
- [ ] Long-running work produces handoffs and does not rely on hidden shell history.

## 6. Observability

- [ ] Structured service logs are documented.
- [ ] Recovery actions are auditable.
- [ ] Deployment history/benchmarks are inspectable.
- [ ] Hardware/model health can be correlated with deployment revisions.

## 7. Security

- [ ] Protected branch / human review is enabled.
- [ ] Secret scanning is part of CI or required validation.
- [ ] Example configs contain no production secrets.
- [ ] State-changing management APIs have tests for authorization.
- [ ] ACMS service-to-service auth is approved before implementation.

## 8. Handoff / continuity

A new agent should be able to answer from the repository alone:

- What does LLM Manager own?
- What does ACMS own?
- How do I discover current models and health?
- How do I place a deployment into maintenance safely?
- How do I preserve and restore a known-good model configuration?
- Where are benchmark/history records represented?
- Which parts are intentionally not implemented yet?
- Which production limitations require extra caution?
