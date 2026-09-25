# Current Sprint

## Sprint Goal

Capture the deployed LLM Manager service into `startupteams/llm-manager-project-framework`, bootstrap the repository with authoritative requirements/architecture/operations documentation, and make the deployed service reproducible without committing secrets or production data.

## Features in Scope

### Deployed service capture

**Requirements**

- `REQ-001`
- `REQ-020`
- `REQ-030`
- `REQ-040`
- `REQ-060`
- `REQ-062`
- `REQ-063`
- `REQ-070`

**Work items**

- [ ] Inventory deployed VM114 source, services, configuration, runtime, database schema, and external dependencies.
- [ ] Copy application source into the repository without secrets, venvs, model files, logs, or production database contents.
- [ ] Capture sanitized service/unit/config examples and reproducible dependency/version information.
- [ ] Add/verify repository validation commands.
- [ ] Run syntax/tests available from the captured project.
- [ ] Run a secret scan before commit.

### Documentation bootstrap

**Requirements**

- `REQ-001` through `REQ-073` as applicable to documented scope.

**Work items**

- [ ] Add populated requirements, future work, architecture, operations, implementation status, ACMS integration, and model-serving docs.
- [ ] Add accepted ADRs for human-directed architecture decisions.
- [ ] Add TDRs for intentionally carried operational debt.
- [ ] Update `AGENTS.md` with LLM Manager-specific safety rules.

## Definition of Done

- [ ] The deployed source needed to reproduce LLM Manager is represented in the repository.
- [ ] No secret-bearing files or production database contents are committed.
- [ ] The README explains the business reason and service boundary.
- [ ] Approved requirements and acceptance criteria are recorded.
- [ ] Future/unapproved work is separated from approved requirements.
- [ ] Current limitations and implementation gaps are explicit.
- [ ] Architecture reflects ACMS/LLM Manager/PDU/inference boundaries.
- [ ] Validation results are reported accurately.
- [ ] Significant decisions are recorded in ADRs.
- [ ] Known intentional debt is recorded in TDRs.
- [ ] Pull request includes a complete handoff and human reviewer focus areas.
- [ ] Human reviewer approves before merge.

## Risks / Blockers / Human Decisions Needed

- An issue/ticket ID is required by repository commit policy; do not invent one.
- The exact ACMS API/authentication contract is intentionally unresolved.
- Production files may contain provider credentials; capture must be sanitize-first.
- The deployed service has evolved through additive production changes, so the repository capture may reveal undocumented schema/runtime dependencies.

## Sprint Handoff

At completion, record captured source locations, files intentionally excluded, current deploy/run/validate commands, database schema/migration status, secrets scan result, tests run, unresolved discrepancies between deployed behavior and repository documentation, and the exact commit/PR.
