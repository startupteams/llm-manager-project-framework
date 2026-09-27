#!/usr/bin/env bash
# rollback.sh — STANDALONE application-only rollback to a previous release (plan §14).
# Usage: rollback.sh [<target-release-sha>]
#
# Policy: application-only. This script NEVER touches the database — DB changes are
# forward-only via migrations (destructive migrations are blocked from ordinary CD by
# policy), so application rollback is safe alongside expand-only schema history.
# Recovery is restarted LAST.
set -uo pipefail
APP_ROOT=/opt/llm-manager
RELEASES=$APP_ROOT/releases
LEDGER=/var/log/llm-manager/release-ledger.jsonl
mkdir -p "$(dirname "$LEDGER")"

CURRENT="$(basename "$(readlink -f "$APP_ROOT/current" 2>/dev/null)" 2>/dev/null || true)"

if [ -n "${1:-}" ]; then TARGET="$1"; else
    # previous = most recent release dir that is not current
    TARGET="$(ls -1t "$RELEASES" 2>/dev/null | grep -v "^$CURRENT$" | head -1 || true)"
fi
[ -n "$TARGET" ] && [ -d "$RELEASES/$TARGET" ] || { echo "FATAL: no rollback target found"; exit 1; }
[ "$TARGET" != "$CURRENT" ] || { echo "FATAL: target == current ($CURRENT)"; exit 1; }

echo "==> rolling back $CURRENT -> $TARGET"
echo '{"ts":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","event":"manual_rollback_start","detail":{"from":"'"$CURRENT"'","to":"'"$TARGET"'"}}' >> "$LEDGER"

systemctl stop llm-manager-recovery || true   # suppress interference first

ln -sfn "$RELEASES/$TARGET" "$APP_ROOT/current.new"
mv -T "$APP_ROOT/current.new" "$APP_ROOT/current"
systemctl daemon-reload

systemctl restart llm-manager-web      || true
systemctl restart llm-manager-collector || true
systemctl restart llm-manager-emporia  || true
systemctl restart llm-manager-recovery || true   # LAST

sleep 3
if "$(dirname "$0")/healthcheck.sh"; then
    echo "==> rollback COMPLETE ($TARGET active)"
    echo '{"ts":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","event":"manual_rollback_complete","detail":{"release":"'"$TARGET"'"}}' >> "$LEDGER"
    exit 0
fi
echo "==> rollback healthcheck FAILED — degraded; human intervention required"
echo '{"ts":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","event":"manual_rollback_degraded","detail":{"release":"'"$TARGET"'"}}' >> "$LEDGER"
exit 1
