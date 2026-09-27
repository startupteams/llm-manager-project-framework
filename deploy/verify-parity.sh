#!/usr/bin/env bash
# verify-parity.sh — compare a host's deployed LLM Manager tree against a release artifact.
# Usage: verify-parity.sh <release-tarball> [--host-root /opt/llm-manager/current]
# Reports file-level md5 parity for app/collectors/control code + ops configs.
set -uo pipefail
TARBALL="${1:?usage: verify-parity.sh <release-tarball> [--host-root PATH]}"; shift || true
HOST_ROOT="/opt/llm-manager/current"
for a in "$@"; do case "$a" in
    --host-root) HOST_ROOT="$(echo "$@" | tr ' ' '\n' | grep -A1 -- --host-root | tail -1)";;
esac; done
# simpler: re-parse
HOST_ROOT="/opt/llm-manager/current"
prev=""; for a in "$@"; do if [ "$prev" = "--host-root" ]; then HOST_ROOT="$a"; fi; prev="$a"; done

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$TARBALL" -C "$STAGE"
REL_SRC="$(find "$STAGE" -maxdepth 1 -type d -name 'llm-manager-*' | head -1)"

MISMATCH=0; MISSING=0; SAME=0
while IFS= read -r rel; do
    case "$rel" in
        release_manifest.json|config/*|docs/*|db/schema/*|db/migrations/*|deploy/*|tests/*) continue ;;
    esac
    if [ ! -f "$HOST_ROOT/$rel" ]; then
        # current/ maps service/app -> app, service/collectors -> collectors, service/control -> control
        alt="$HOST_ROOT/${rel#service/}"
        if [ -f "$alt" ]; then
            if [ "$(md5sum < "$REL_SRC/$rel" | cut -d' ' -f1)" = "$(md5sum < "$alt" | cut -d' ' -f1)" ]; then
                SAME=$((SAME+1))
            else
                echo "MISMATCH: $rel"; MISMATCH=$((MISMATCH+1))
            fi
        else
            echo "MISSING on host: $rel"; MISSING=$((MISSING+1))
        fi
        continue
    fi
    if [ "$(md5sum < "$REL_SRC/$rel" | cut -d' ' -f1)" = "$(md5sum < "$HOST_ROOT/$rel" | cut -d' ' -f1)" ]; then
        SAME=$((SAME+1))
    else
        echo "MISMATCH: $rel"; MISMATCH=$((MISMATCH+1))
    fi
done < <(cd "$REL_SRC" && find service ops -type f | sort)

echo "parity: same=$SAME mismatch=$MISMATCH missing=$MISSING"
[ "$MISMATCH" = "0" ] && [ "$MISSING" = "0" ] && { echo "==> PARITY OK"; exit 0; }
echo "==> PARITY DRIFT DETECTED"; exit 1
