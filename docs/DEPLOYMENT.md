# LLM Manager — Deployment Runbook

## Target layout (plan §2)

```
/opt/llm-manager/
├── releases/<git-sha>/          # immutable trees (app, collectors, control, ops, db, config)
├── current -> releases/<sha>    # activation = symlink swap
└── shared/{logs,state}          # survives releases
/etc/llm-manager/                # mutable: secrets/, tls/, litellm.env, generated litellm_config.yaml
```

Systemd units are installed pointing at `/opt/llm-manager/current/...` — the
symkink swap is the atomic-ish activation; `git pull` into production is banned.

## Fresh-VM bootstrap (clean-room, plan §5–§7)

```
# on a fresh Ubuntu 24.04 VM with the artifact staged:
sudo ./deploy/bootstrap-vm.sh llm-manager-<sha12>.tar.gz --staging
```

Installs packages, Python 3.12 (fails otherwise), release venv from the pinned
freeze, systemd units, nginx site (generates staging self-signed TLS), AgentManager.
The gate (plan §7): staging must come up from `GitHub artifact + documented
external secrets + documented external endpoints` ONLY — any undocumented manual
file/edit fails the hostability gate. Reboot staging and re-run `healthcheck.sh`
to prove persistence.

## Production deploy (plan §13)

```
# 1. build + checksum (CI does this; manual escape hatch:)
./deploy/build-release.sh

# 2. preflight
./deploy/preflight.sh dist/llm-manager-<sha12>.tar.gz

# 3. full transaction (preflight -> snapshot -> stop recovery -> install ->
#    validate -> migrations -> activate -> restart w/ recovery LAST -> smoke ->
#    auto-rollback on failure)
sudo ./deploy/deploy-release.sh dist/llm-manager-<sha12>.tar.gz
```

Key rules enforced by the transaction:
- `llm-manager-recovery` is stopped FIRST and started LAST (plan §13).
- Hosted model servers are never touched by a manager deploy.
- `litellm` restart in prod is deferred to the routing-regeneration policy (§15);
  the release's `litellm_sync.py` regenerates config from DB, never a stale capture.
- Migrations run through `db/migrations/migrate.py` (ledger-backed, forward-only).

## Rollback (plan §14)

Automatic when smoke fails inside `deploy-release.sh`. Manual escape hatch:

```
sudo ./deploy/rollback.sh [<target-sha>]
```

Application-only: symlink swap back + service restarts (recovery last). The DB is
NOT restored by this script — see migration policy.

## Parity verification

```
./deploy/verify-parity.sh dist/llm-manager-<sha12>.tar.gz
```

Compares the running tree against the artifact; use after any suspected drift.

## Observability

- `/healthz` — `{"ok": true, "version": "..."}` (nginx :80 stays plain for probes).
- `release_manifest.json` in every release carries `git_sha` + `build_time`.
- `/api/status` version field reports the running app version.
- Release ledger: `/var/log/llm-manager/release-ledger.jsonl` (activate/rollback events).

## Secret provisioning (values never enter Git/CI)

Provision out-of-band on the target host under `/etc/llm-manager/secrets/` with the
exact filenames in `deploy/README.md`; `OPENROUTER_API_KEY` goes in
`/etc/llm-manager/litellm.env`. TLS pair under `/etc/llm-manager/tls/`.

## Pre-migration DB backup (operator, plan §8)

Before ANY production migration: take a protected local backup (e.g.
`pg_dump -Fc` into root-owned 0600 on the DB host). Never upload production DB
dumps to GitHub or CI artifacts.
