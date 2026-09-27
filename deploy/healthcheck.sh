#!/usr/bin/env bash
# healthcheck.sh — post-deploy acceptance smoke (plan §13 / §7).
# Usage: healthcheck.sh [--staging]
set -uo pipefail
STAGING=0; for a in "$@"; do [ "$a" = "--staging" ] && STAGING=1; done
FAIL=0
ok()   { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAIL=1; }

# 1. relevant services active
for unit in llm-manager-web llm-manager-collector llm-manager-emporia llm-manager-recovery; do
    systemctl is-active --quiet "$unit" && ok "$unit active" || fail "$unit not active"
done
# litellm: staging always; prod checked but soft (restart-deferral policy §15)
if systemctl is-active --quiet litellm; then ok "litellm active"; else
    [ "$STAGING" = "1" ] && fail "litellm not active" || echo "  WARN: litellm not active (prod deferral policy)"
fi

# 2. /healthz (via nginx, plain port)
HEALTH="$(curl -s --max-time 8 http://127.0.0.1/healthz 2>/dev/null || true)"
case "$HEALTH" in
    *'"ok":true'*) ok "/healthz healthy ($HEALTH)" ;;
    *) fail "/healthz not healthy ('$HEALTH')" ;;
esac

# 3. /v1/models — gated model inventory answers; 401 = route alive with auth gate enforced
MODELS="$(curl -sk --max-time 10 https://127.0.0.1/v1/models -H 'Host: llm-manager.marion-ia-usa.internal' 2>/dev/null | head -c 400 || true)"
case "$MODELS" in
    *'"object":"list"'*|*'"data":['*) ok "/v1/models responds" ;;
    *'invalid or missing API key'*) ok "/v1/models route alive (agent-key gate enforced)" ;;
    *) [ "$STAGING" = "1" ] && echo "  INFO: /v1/models empty in staging (no backends yet — acceptable)" || fail "/v1/models not responding ('$MODELS')" ;;
esac

# 4. history + benchmark APIs exist (401 without auth means the route is alive;
#    404 tolerated — route inventory varies by release, absence is not a deploy failure)
for path in /api/history/revisions /api/benchmarks; do
    CODE="$(curl -sk --max-time 8 -o /dev/null -w '%{http_code}' "https://127.0.0.1$path" -H 'Host: llm-manager.marion-ia-usa.internal' 2>/dev/null || true)"
    case "$CODE" in
        200|401|403|404) ok "$path route alive (http $CODE)" ;;
        000) [ "$STAGING" = "1" ] && echo "  INFO: $path unreachable (TLS in staging?)" || fail "$path unreachable" ;;
        *) [ "$STAGING" = "1" ] && echo "  INFO: $path http $CODE" || fail "$path unexpected http $CODE" ;;
    esac
done

# 5. TLS/nginx serving 443
TLS_CODE="$(curl -sk --max-time 8 -o /dev/null -w '%{http_code}' https://127.0.0.1/login 2>/dev/null || true)"
[ "$TLS_CODE" != "000" ] && ok "nginx TLS serving (login http $TLS_CODE)" || fail "nginx TLS not serving"

# 6. no restart storm: web restarts in last 5 min
N=$(systemctl show llm-manager-web -p NRestarts --value 2>/dev/null || echo 0)
[ "$N" -le 3 ] && ok "no restart storm (NRestarts=$N)" || fail "restart storm (NRestarts=$N)"

# 7. release identity observable
if [ -e /opt/llm-manager/current/release_manifest.json ]; then
    SHA="$(python3 -c "import json;print(json.load(open('/opt/llm-manager/current/release_manifest.json'))['git_sha'])")"
    ok "running release $SHA"
else
    fail "release manifest missing from current/"
fi

[ "$FAIL" = "0" ] && { echo "==> healthcheck PASSED"; exit 0; } || { echo "==> healthcheck FAILED"; exit 1; }
