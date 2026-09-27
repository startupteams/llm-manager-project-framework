#!/usr/bin/env bash
# preflight.sh — pre-deployment verification (plan §13 preflight).
# Usage: preflight.sh <release-tarball> [--staging]
set -euo pipefail

TARBALL="${1:?usage: preflight.sh <release-tarball> [--staging]}"
STAGING=0; for a in "$@"; do [ "$a" = "--staging" ] && STAGING=1; done
APP_ROOT=/opt/llm-manager
FAIL=0
ok()   { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAIL=1; }

echo "==> preflight for $(basename "$TARBALL")"

# artifact checksum
if [ -f "${TARBALL}.sha256" ] && (cd "$(dirname "$TARBALL")" && sha256sum -c "$(basename "${TARBALL}.sha256")" >/dev/null 2>&1); then
    ok "artifact checksum"
else fail "artifact checksum"; fi

# release identity
REL_SHA="$(tar -xzOf "$TARBALL" --wildcards '*/release_manifest.json' 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["git_sha"])' 2>/dev/null || true)"
[ -n "$REL_SHA" ] && ok "release manifest (git_sha=${REL_SHA:0:12})" || fail "release manifest unreadable"

# current release record
if [ -e "$APP_ROOT/current" ]; then
    ok "current release: $(readlink "$APP_ROOT/current" | xargs basename 2>/dev/null || readlink "$APP_ROOT/current")"
else
    [ "$STAGING" = "1" ] && echo "  INFO: no current release yet (fresh staging)" || fail "no current release symlink"
fi

# disk space (need >= 2 GiB free on /)
AVAIL=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
[ "$AVAIL" -ge 2 ] && ok "disk space (${AVAIL}G free)" || fail "disk space (${AVAIL}G free < 2G)"

# python runtime compatibility
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
[ "$PYVER" = "3.12" ] && ok "python ${PYVER}" || fail "python 3.12 required, found '${PYVER:-none}'"

# external files: secrets dir present (names only check)
if [ -d /etc/llm-manager/secrets ] && [ -n "$(ls -A /etc/llm-manager/secrets 2>/dev/null)" ]; then
    ok "secrets directory populated"
else
    [ "$STAGING" = "1" ] && echo "  INFO: secrets dir empty (staging may bootstrap later)" || fail "secrets directory empty"
fi

# DB reachable
if python3 - <<'PY' >/dev/null 2>&1
import os, psycopg2
dsn = os.environ.get("LLM_MANAGER_PG_DSN")
if not dsn:
    dsn = f"host={os.environ.get('LLM_MANAGER_PG_HOST','10.0.20.116')} dbname={os.environ.get('LLM_MANAGER_PG_DB','llmmanager')} user={os.environ.get('LLM_MANAGER_PG_USER','llmmanager')} password={os.environ.get('LLM_MANAGER_PG_PASSWORD','')}"
psycopg2.connect(dsn, connect_timeout=4).close()
PY
then ok "postgres reachable"; else [ "$STAGING" = "1" ] && echo "  INFO: postgres unreachable (staging may configure later)" || fail "postgres unreachable"; fi

# nginx + systemd state
if command -v nginx >/dev/null && nginx -t >/dev/null 2>&1; then ok "nginx config valid"; else [ "$STAGING" = "1" ] && echo "  INFO: nginx not ready (staging)" || fail "nginx config invalid"; fi
if systemctl is-active llm-manager-web >/dev/null 2>&1; then ok "llm-manager-web active"; else [ "$STAGING" = "1" ] && echo "  INFO: web not running yet" || echo "  INFO: llm-manager-web not active (first deploy?)"; fi

# current healthz
HEALTH="$(curl -sk --max-time 5 http://127.0.0.1/healthz 2>/dev/null || true)"
case "$HEALTH" in
    *'"ok":true'*) ok "current /healthz healthy" ;;
    "") [ "$STAGING" = "1" ] && echo "  INFO: no current healthz (fresh)" || fail "current /healthz unreachable" ;;
    *) [ "$STAGING" = "1" ] && echo "  INFO: healthz unexpected: $HEALTH" || fail "current /healthz unexpected: $HEALTH" ;;
esac

[ "$FAIL" = "0" ] && { echo "==> preflight PASSED"; exit 0; } || { echo "==> preflight FAILED"; exit 1; }
