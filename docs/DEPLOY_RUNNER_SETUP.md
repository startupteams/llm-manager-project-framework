# Self-hosted deploy runner setup (plan §10)

## Dedicated runner host

Small MARION VM/LXC (NOT VM114), labels: `self-hosted, marion, llm-manager-deploy`.

Rules (plan §10):
- scoped to `startupteams/llm-manager-project-framework` only;
- NO PR jobs — only deployment workflows (staging CD + production CD);
- network access to staging (10.0.20.131) and VM114 (10.0.20.108);
- no application/database secrets on the runner;
- dedicated SSH deploy key, not a human key;
- no GPU/model-server role.

## Restricted deploy account on targets (VM114 + staging)

```
# create account + wrappers on the target:
useradd -m -s /bin/bash llm-manager-deploy
mkdir -p /opt/llm-manager-deploy
# wrappers are root-owned, sudo-limited, reviewed:
#   bootstrap-wrapper.sh  -> exec /opt/llm-manager-deploy/bootstrap-vm.sh "$@"
#   deploy-wrapper.sh     -> exec /opt/llm-manager-deploy/deploy-release.sh "$@"
#   healthcheck-wrapper.sh-> exec /opt/llm-manager-deploy/healthcheck.sh "$@"
cat > /etc/sudoers.d/llm-manager-deploy <<'EOF'
llm-manager-deploy ALL=(root) NOPASSWD: /opt/llm-manager-deploy/bootstrap-wrapper.sh, /opt/llm-manager-deploy/deploy-wrapper.sh, /opt/llm-manager-deploy/healthcheck-wrapper.sh
EOF
chmod 440 /etc/sudoers.d/llm-manager-deploy
```

Deploy key: generate on the runner, append the public key to
`/home/llm-manager-deploy/.ssh/authorized_keys` on both targets. No secrets
beyond that key live on the runner.

## GitHub-side

- Runner registered at repo level, labels `self-hosted,marion,llm-manager-deploy`.
- Environment `production` (Settings → Environments) with **required reviewer** —
  this is the human-approval gate for §12.
- Branch protection on `main`: PR-only, required CI checks (see docs/CI_CD.md).

## Why wrappers + sudo allowlist

The runner never gets root SSH; it may only execute the three reviewed
deployment scripts. Everything else is denied — the blast radius of a
compromised runner token is bounded to "deploy or healthcheck a release".
