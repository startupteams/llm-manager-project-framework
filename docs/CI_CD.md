# LLM Manager — CI/CD

Pipeline: `PR -> CI -> merge -> auto staging -> human approval -> production`.

## CI (`.github/workflows/ci.yml`) — GitHub-hosted runners

| Job | Content |
|---|---|
| `syntax` | `py_compile` all service Python |
| `tests` | `pytest tests/` (§21.8 switching regressions) + `v011_cost_tests.py` |
| `secret-scan` | gitleaks + custom DSN/private-key patterns |
| `deps` | clean venv install from pinned freeze (fails if not reproducible) + `pip-audit` (HIGH+ fails; findings triaged in issues) |
| `config` | YAML parse (litellm reference config), systemd unit sanity, `nginx -t` with temp certs |
| `db` | ephemeral PostgreSQL service: apply `db/schema/schema.sql` + `db/migrations/migrate.py apply/verify` + smoke queries |
| `integration-smoke` | run FastAPI app with mock DB/LDAP env: `/healthz`, gated `/v1/models`, desired-state regression endpoints |
| `artifact` | `deploy/build-release.sh` -> `llm-manager-<sha>.tar.gz` (+ sha256), uploaded artifact |

Arbitrary PR code never runs on MARION self-hosted runners (plan §9).

## Staging CD (`.github/workflows/deploy-staging.yml`) — self-hosted runner

Trigger: successful `artifact` job on `main`. The self-hosted runner (dedicated
MARION VM/LXC, scoped to this repo, no PR jobs, no app/DB secrets, dedicated SSH
deploy key, no GPU role) downloads the EXACT CI artifact, verifies checksums,
transfers to staging, and runs `deploy/deploy-release.sh --staging`.

## Production CD (`.github/workflows/deploy-production.yml`) — protected environment

Trigger: `workflow_dispatch` (later: environment approval after staging success).
GitHub Environment `production` requires a human reviewer. Production deploys the
SAME artifact/SHA that passed staging — never rebuilt.

## Release identity

Git SHA everywhere: artifact name, release directory, `release_manifest.json`,
migration ledger rows. `/healthz` returns version; build metadata (git_sha,
build_time) ships in the release manifest and is surfaced via `/api/status`
version field (see docs/DEPLOYMENT.md §observability).

## Rollback

Application-only, automatic on smoke failure (plan §14). DB changes are
forward-only; destructive migrations are named `NNN_destructive_*.sql` and the
migration runner refuses them outside an explicit maintenance procedure.

## Generated LiteLLM config policy (§15)

`ops/litellm/litellm_config.yaml` in the repo is a REFERENCE CAPTURE. Production
routing is regenerated from LLM Manager DB state via `litellm_sync.py`. Deploy
tooling never copies the captured yaml over production. Route/model counts are
compared before/after regeneration and any unexpected loss fails the smoke.
