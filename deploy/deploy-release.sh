#!/usr/bin/env bash
# deploy-release.sh — the LLM Manager release transaction (plan §13).
# Sequence: preflight -> snapshot -> suppress recovery -> install candidate ->
# candidate validation -> migrations -> activate -> restart (recovery LAST) -> smoke
# -> commit, or automatic application rollback on smoke failure (plan §14).
# Usage: deploy-release.sh <release-tarball> [--staging] [--skip-migrations]
set -uo pipefail

TARBALL="${1:?usage: deploy-release.sh <release-tarball> [--staging] [--skip-migrations]}"; shift || true
STAGING=0; SKIP_MIG=0
for a in "$@"; do
  [ "$a" = "--staging" ] && STAGING=1
  [ "$a" = "--skip-migrations" ] && SKIP_MIG=1
done

APP_ROOT=/opt/llm-manager
RELEASES=$APP_ROOT/releases
LEDGER=/var/log/llm-manager/release-ledger.jsonl
mkdir -p "$(dirname "$LEDGER")" "$APP_ROOT/shared/logs" "$APP_ROOT/shared/state"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
ledger() { echo "{\"ts\":\"$(stamp)\",\"event\":\"$1\",\"detail\":$2}" >> "$LEDGER"; }

die() { echo "FATAL: $*" >&2; ledger "abort" "\"$*\""; exit 1; }

echo "==> LLM Manager release transaction ($(stamp))"

# ---- Phase A: preflight --------------------------------------------------------
# First promotion of the flat layout (prod, no current symlink yet): allow the
# missing-current preflight check as a WARN for this one transaction.
PROMOTION=0
if [ "$STAGING" = 0 ] && [ ! -e "$APP_ROOT/current" ]; then
    PROMOTION=1
    export LLM_MANAGER_FIRST_PROMOTION=1
fi
"$(dirname "$0")/preflight.sh" "$TARBALL" $([ "$STAGING" = 1 ] && echo --staging) || die "preflight failed"

# snapshot current state
PREV_REL=""
[ -e "$APP_ROOT/current" ] && PREV_REL="$(basename "$(readlink -f "$APP_ROOT/current")")"
PREV_HEALTH="$(curl -sk --max-time 5 http://127.0.0.1/healthz 2>/dev/null || true)"
ledger "snapshot" "{\"previous_release\":\"$PREV_REL\",\"health\":$(echo "${PREV_HEALTH:-null}" | python3 -c 'import sys,json; s=sys.stdin.read().strip(); print(s if s.startswith("{") else "null")')}"
echo "==> snapshot: previous release ${PREV_REL:-none}"

# ---- Phase B: unpack + verify artifact -----------------------------------------
STAGE="$(mktemp -d)"
tar -xzf "$TARBALL" -C "$STAGE"
REL_SRC="$(find "$STAGE" -maxdepth 1 -type d -name 'llm-manager-*' | head -1)"
[ -n "$REL_SRC" ] || die "unexpected artifact layout"
REL_SHA="$(python3 -c "import json;print(json.load(open('$REL_SRC/release_manifest.json'))['git_sha'])")"
REL_DST="$RELEASES/$REL_SHA"

# verify manifest checksums of what we unpacked
python3 - "$REL_SRC" <<'PY' || die "artifact manifest verification failed"
import hashlib, json, os, sys
stage = sys.argv[1]
man = json.load(open(os.path.join(stage, "release_manifest.json")))
bad = []
for rel, expect in man["files"].items():
    p = os.path.join(stage, rel)
    if not os.path.exists(p) or hashlib.sha256(open(p, "rb").read()).hexdigest() != expect:
        bad.append(rel)
if bad:
    print("corrupt artifact files:", bad[:5]); sys.exit(1)
print(f"manifest verified: {len(man['files'])} files")
PY

# ---- Phase C: suppress interference (recovery FIRST to stop) --------------------
if systemctl is-active llm-manager-recovery >/dev/null 2>&1; then
    systemctl stop llm-manager-recovery && echo "==> recovery stopped (suppressed for deploy)"
    ledger "recovery_stopped" "null"
fi
# NOTE: hosted model servers are NEVER touched by a manager deploy (plan §13).

# ---- Phase D: install candidate --------------------------------------------------
if [ -d "$REL_DST" ]; then
    echo "==> candidate $REL_SHA already present (re-release of same SHA)"
else
    mkdir -p "$REL_DST"
    cp -a "$REL_SRC"/. "$REL_DST"/
    echo "==> candidate installed at $REL_DST"
fi
rm -rf "$STAGE"

# release venv (idempotent)
VENV="$REL_DST/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r "$REL_DST/config/examples/requirements-v011-venv-freeze.txt"
fi
# LiteLLM's Prisma client needs generated binaries in EVERY fresh release venv
# (found live: fresh release venv 500s litellm at startup with "Unable to find
# Prisma binaries" until `prisma generate` runs — node is preinstalled on the VMs).
PRISMA_SCHEMA="$VENV/lib/python3.12/site-packages/litellm/proxy/schema.prisma"
if [ -f "$PRISMA_SCHEMA" ] && command -v node >/dev/null 2>&1; then
    PRISMA_MARKER="$VENV/.prisma-generated"
    if [ ! -f "$PRISMA_MARKER" ]; then
        echo "==> prisma generate (release venv)"
        (cd "$(dirname "$PRISMA_SCHEMA")" && \
            PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin" \
            "$VENV/bin/prisma" generate --schema schema.prisma >/dev/null 2>&1) \
            && touch "$PRISMA_MARKER" \
            || die "prisma generate failed"
    fi
fi
for d in logs state; do [ -e "$REL_DST/$d" ] || ln -s "$APP_ROOT/shared/$d" "$REL_DST/$d"; done

# legacy path compat symlinks (same as bootstrap §5b)
ln -sfn "$REL_DST/service/app"          "$REL_DST/app"
ln -sfn "$REL_DST/service/collectors"   "$REL_DST/collectors"
ln -sfn "$REL_DST/service/control"      "$REL_DST/control"
ln -sfn "$REL_DST/service/agentmanager" "$REL_DST/agentmanager"
ln -sfn "$REL_DST/.venv"                "$REL_DST/venv"

# install/refresh unit files pointing at current/
for unit in "$REL_DST"/ops/systemd/*.service; do
    sed -e 's#/opt/llm-manager/app#/opt/llm-manager/current/app#g' \
        -e 's#/opt/llm-manager/venv#/opt/llm-manager/current/venv#g' \
        -e 's#/opt/llm-manager/collectors#/opt/llm-manager/current/collectors#g' \
        "$unit" > "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload

# ---- Phase E: candidate validation (before activation) ----------------------------
echo "==> candidate validation"
"$VENV/bin/python" -m py_compile "$REL_DST"/service/app/*.py "$REL_DST"/service/collectors/*.py "$REL_DST"/service/control/*.py \
    || die "candidate py_compile failed"
"$VENV/bin/python" "$REL_DST/service/app/v011_cost_tests.py" >/dev/null || die "candidate cost tests failed"
"$VENV/bin/python" -c "import sys; sys.path.insert(0,'$REL_DST/service/app'); import ast; [ast.parse(open(f).read()) for f in ('$REL_DST/service/app/main.py','$REL_DST/service/app/v011_core.py','$REL_DST/service/app/v011_recovery.py','$REL_DST/service/app/litellm_sync.py')]" || die "candidate AST parse failed"
echo "  candidate validation passed"

# ---- Phase F: migrations -----------------------------------------------------------
if [ "$SKIP_MIG" = "0" ] && [ -d "$REL_DST/db/migrations" ]; then
    echo "==> migrations"
    # DB target resolution is INSIDE migrate.py: env override > pg_app_creds file
    # (PG_HOST/PG_DB/PG_USER/PG_PW) > local default. Do NOT pre-set env vars here
    # with defaults — an exported 127.0.0.1 would defeat migrate.py's file contract
    # (the first production promotion hit exactly that). Only values the operator
    # explicitly exported before the transaction reach migrate.py as env.
    UNIT_ENV="$(systemctl show llm-manager-web -p Environment --value 2>/dev/null || true)"
    UNIT_HOST="$(echo "$UNIT_ENV" | tr ' ' '\n' | sed -n 's/^LLM_MANAGER_PG_HOST=//p' | head -1)"
    UNIT_USER="$(echo "$UNIT_ENV" | tr ' ' '\n' | sed -n 's/^LLM_MANAGER_PG_USER=//p' | head -1)"
    UNIT_PW="$(echo "$UNIT_ENV" | tr ' ' '\n' | sed -n 's/^LLM_MANAGER_PG_PASSWORD=//p' | head -1)"
    [ -n "$UNIT_HOST" ] && export LLM_MANAGER_PG_HOST="$UNIT_HOST"
    [ -n "$UNIT_USER" ] && export LLM_MANAGER_PG_USER="$UNIT_USER"
    [ -n "$UNIT_PW" ] && export LLM_MANAGER_PG_PASSWORD="$UNIT_PW"
    "$VENV/bin/python" "$REL_DST/db/migrations/migrate.py" apply --git-sha "$REL_SHA" \
        || { echo "migrations failed — rolling back candidate install"; rm -rf "$REL_DST"; die "migration failure"; }
else
    echo "==> migrations skipped (skip=$SKIP_MIG)"
fi

# ---- Phase G: activate --------------------------------------------------------------
ln -sfn "$REL_DST" "$APP_ROOT/current.new"
mv -T "$APP_ROOT/current.new" "$APP_ROOT/current"   # atomic-ish swap
echo "==> activated $REL_SHA (previous: ${PREV_REL:-none})"
ledger "activate" "{\"release\":\"$REL_SHA\",\"previous\":\"$PREV_REL\"}"

# ---- Phase H: restart services (recovery LAST) ---------------------------------------
restart() { systemctl restart "$1" && echo "  restarted $1" || return 1; }
FAILHERE=0
restart llm-manager-web      || FAILHERE=1
restart llm-manager-collector || true
restart llm-manager-emporia  || true
# LiteLLM: only restart if its generated config was regenerated this deploy;
# routing regeneration is the release's litellm_sync.py (never a stale captured yaml)
if [ "$STAGING" = "1" ]; then
    restart litellm || FAILHERE=1
else
    echo "  (litellm restart deferred to routing-regeneration policy §15)"
fi
systemctl reload nginx 2>/dev/null || true
restart llm-manager-recovery || FAILHERE=1   # recovery LAST

# ---- Phase I: smoke ------------------------------------------------------------------
sleep 3
if "$(dirname "$0")/healthcheck.sh" $([ "$STAGING" = 1 ] && echo --staging); then
    echo "==> Release ACCEPTED ($REL_SHA)"
    ledger "accepted" "{\"release\":\"$REL_SHA\"}"
    exit 0
fi

echo "==> smoke FAILED — automatic application rollback (plan §14)"
ledger "rollback_start" "{\"from\":\"$REL_SHA\",\"to\":\"${PREV_REL:-none}\"}"
if [ -n "$PREV_REL" ] && [ -d "$RELEASES/$PREV_REL" ]; then
    ln -sfn "$RELEASES/$PREV_REL" "$APP_ROOT/current.new"
    mv -T "$APP_ROOT/current.new" "$APP_ROOT/current"
    systemctl daemon-reload
    systemctl restart llm-manager-web || true
    systemctl restart llm-manager-collector || true
    systemctl restart llm-manager-emporia || true
    systemctl restart llm-manager-recovery || true
    sleep 3
    if "$(dirname "$0")/healthcheck.sh"; then
        echo "==> ROLLBACK COMPLETE — back on $PREV_REL"
        ledger "rollback_complete" "{\"release\":\"$PREV_REL\"}"
    else
        echo "==> ROLLBACK healthcheck still failing — HUMAN INTERVENTION REQUIRED"
        ledger "rollback_degraded" "{\"release\":\"$PREV_REL\"}"
    fi
else
    echo "==> no previous release to roll back to — manual recovery required"
    ledger "rollback_impossible" "null"
fi
exit 1
