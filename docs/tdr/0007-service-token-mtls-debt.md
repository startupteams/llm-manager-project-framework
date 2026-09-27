# TDR-0007 — scoped bearer tokens instead of mTLS for service auth

Status: Accepted (REV4 §11 names this debt explicitly)

## Context

Server Manager machine API (§10) needs service-to-service auth for ACMS ↔ Server Manager.
mTLS is the target architecture but is deferred (§19).

## Decision

Bearer credentials from a root-owned 0600 file (`/etc/llm-manager/secrets/service_tokens`),
parsed as `service=token:scope1,scope2`. Constant-time token compare; 401-first ordering;
scopes are least-privilege (runtime:read/write/destroy, job:read/submit, route:read,
usage:read, health:read). Rotation = edit file + restart (mtime-triggered reload).

## Consequences

- Token compromise is bounded by scope + private network + audit.
- Rotation is manual until a credential broker exists (future work per §10A GitHub-identity note).
