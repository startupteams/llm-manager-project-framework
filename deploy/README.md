# deploy/ — LLM Manager release tooling

Immutable-release deployment for VM114 + staging. See `docs/DEPLOYMENT.md` for the
full runbook and `docs/CI_CD.md` for the pipeline description.

## Layout this implements

```
/opt/llm-manager/
├── releases/<git-sha>/        # immutable release trees (app, collectors, control, ops)
├── current -> releases/<sha>  # atomic-ish promotion via symlink swap
└── shared/                    # logs, state that survives releases

/etc/llm-manager/              # mutable + secrets — NEVER inside a release
├── secrets/                   # values live only on the host (see secret contract below)
├── tls/
├── litellm.env
└── litellm_config.yaml        # GENERATED runtime state (litellm_sync.py) — never blind-overwritten
```

## Scripts

| Script | Purpose |
|---|---|
| `build-release.sh` | Build `llm-manager-<sha>.tar.gz` from a Git worktree; excludes secrets/venvs/logs/DB data; writes `release_manifest.json` with SHA + checksums |
| `bootstrap-vm.sh` | Idempotent fresh-Ubuntu-24.04 install: packages, Python 3.12, release-specific venv from pinned freeze, systemd units, nginx site, directories. **No secrets embedded** — secrets are supplied out-of-band per the contract |
| `preflight.sh` | Verify artifact checksum, disk space, current release, systemd/nginx state, DB/LLDAP reachability, current `/healthz` |
| `deploy-release.sh` | The release transaction: preflight → stop recovery → install release → venv → candidate validation → migrations → activate → restart services (recovery LAST) → smoke → commit or auto-rollback |
| `healthcheck.sh` | Post-deploy acceptance: units active, `/healthz`, `/v1/models`, history/benchmark APIs, nginx TLS, optional model completion |
| `rollback.sh` | Application-only rollback to previous release (symlink swap + restart, recovery last). Refuses DB restore — DB changes are forward-only via migrations |
| `verify-parity.sh` | Compare a target host's deployed tree + configs against a release artifact (md5-based) |

## Migration runner

`db/migrations/` holds numbered, forward-only SQL files plus `migrate.py`
(ledger table `schema_migrations`: version, checksum, applied_at, git_sha).
`schema.sql` remains the clean-install reference only.

## Secret contract (names only — values never enter Git or CI)

`/etc/llm-manager/secrets/`: `session_key`, `litellm_master_key`, `ldap_bind_dn`,
`ldap_bind_pw`, `ldap_base_dn`, `pg_app_creds` (format `PG_PW=<pw>` line),
`pve_token`, `service_token`, `openrouter_api_key`, `emporia_creds`,
`litellm_salt`, `keys/` (ssh key for recovery remote ops).
`/etc/llm-manager/litellm.env`: `OPENROUTER_API_KEY=<value>`.
