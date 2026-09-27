#!/usr/bin/env bash
# bootstrap-vm.sh — idempotent fresh-Ubuntu-24.04 installer for an LLM Manager release artifact.
# Usage: bootstrap-vm.sh <release-tarball> [--staging]
#
# Installs: OS packages, Python 3.12, release venv from pinned freeze, systemd units,
# nginx site, directories, AgentManager service. Does NOT embed or create secret VALUES —
# secrets must be provisioned out-of-band under /etc/llm-manager/secrets/ per deploy/README.md.
#
# Staging mode (--staging): uses staging service suffixes so staging can coexist with prod tooling.
set -euo pipefail

TARBALL="$1"; shift || true
STAGING=0
for arg in "$@"; do [ "$arg" = "--staging" ] && STAGING=1; done

[ -f "$TARBALL" ] || { echo "usage: $0 <release-tarball> [--staging]"; exit 1; }

APP_ROOT="/opt/llm-manager"
ETC_ROOT="/etc/llm-manager"
RELEASES="$APP_ROOT/releases"
SHARED="$APP_ROOT/shared"

SUF=""; [ "$STAGING" = "1" ] && SUF="-staging"

echo "==> LLM Manager bootstrap (staging=$STAGING)"

# --- 1. OS packages -----------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv python3-pip nginx postgresql-client \
    curl ca-certificates >/dev/null

PY="$(command -v python3.12 || command -v python3)"
PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[ "$PYVER" = "3.12" ] || { echo "FATAL: Python 3.12.x required, found $PYVER"; exit 1; }

# --- 2. Directory layout ------------------------------------------------------
install -d -m 0755 "$RELEASES" "$SHARED" "$APP_ROOT"
install -d -m 0750 "$ETC_ROOT" "$ETC_ROOT/secrets" "$ETC_ROOT/tls" "$ETC_ROOT/backups"
install -d -m 0755 /var/log/llm-manager

# --- 3. Verify + unpack release artifact --------------------------------------
SHA256_FILE="${TARBALL}.sha256"
if [ -f "$SHA256_FILE" ]; then
    (cd "$(dirname "$TARBALL")" && sha256sum -c "$(basename "$SHA256_FILE")") \
        || { echo "FATAL: artifact checksum mismatch"; exit 1; }
fi
STAGE_DIR="$(mktemp -d)"
tar -xzf "$TARBALL" -C "$STAGE_DIR"
REL_SRC="$(find "$STAGE_DIR" -maxdepth 1 -type d -name 'llm-manager-*' | head -1)"
[ -n "$REL_SRC" ] || { echo "FATAL: unexpected artifact layout"; exit 1; }
REL_SHA="$(python3 -c "import json;print(json.load(open('$REL_SRC/release_manifest.json'))['git_sha'])")"
REL_DST="$RELEASES/$REL_SHA"

if [ -d "$REL_DST" ]; then
    echo "==> release $REL_SHA already installed (idempotent re-run)"
else
    mkdir -p "$REL_DST"
    cp -a "$REL_SRC"/. "$REL_DST"/
    rm -rf "$STAGE_DIR"
    echo "==> installed release $REL_SHA"
fi

# --- 4. Release venv from pinned freeze ----------------------------------------
VENV="$REL_DST/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    FREEZE="$REL_DST/config/examples/requirements-v011-venv-freeze.txt"
    "$VENV/bin/pip" install --quiet -r "$FREEZE"
    echo "==> venv built from pinned freeze ($(wc -l < "$FREEZE") pins)"
fi

# --- 5. Shared state symlinks inside release -----------------------------------
# runtime-writable paths point at shared/ so releases stay immutable
for d in logs state; do
    [ -e "$REL_DST/$d" ] || ln -s "$SHARED/$d" "$REL_DST/$d"
done
install -d "$SHARED/logs" "$SHARED/state"

# --- 5b. Legacy path compat symlinks -------------------------------------------
# systemd units + app code reference flat names (app/, collectors/, control/,
# venv/) while the artifact nests under service/ and builds .venv. Symlink once
# per release so unit files written for /opt/llm-manager/current/... resolve.
ln -sfn "$REL_DST/service/app"        "$REL_DST/app"
ln -sfn "$REL_DST/service/collectors" "$REL_DST/collectors"
ln -sfn "$REL_DST/service/control"    "$REL_DST/control"
ln -sfn "$REL_DST/service/agentmanager" "$REL_DST/agentmanager"
ln -sfn "$REL_DST/.venv"              "$REL_DST/venv"

# --- 6. systemd units ----------------------------------------------------------
# Units point at /opt/llm-manager/current/... so activation = symlink swap.
for unit in "$REL_DST"/ops/systemd/*.service; do
    sed -e 's#/opt/llm-manager/app#/opt/llm-manager/current/app#g' \
        -e 's#/opt/llm-manager/venv#/opt/llm-manager/current/venv#g' \
        -e 's#/opt/llm-manager/collectors#/opt/llm-manager/current/collectors#g' \
        "$unit" > "/etc/systemd/system/$(basename "$unit")"
done
# Drop-in: service suffix for staging coexistence
mkdir -p "/etc/systemd/system/litellm.service.d"
cat > /etc/systemd/system/litellm.service.d/env.conf <<EOF
[Service]
EnvironmentFile=$ETC_ROOT/litellm.env
EOF

# agent-manager unit (captured from prod; runs on the manager venv)
cat > /etc/systemd/system/agent-manager.service <<EOF
[Unit]
Description=Startup Teams Agent Manager (V2 vertical slice)
After=network-online.target llm-manager-web.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/agent-manager
ExecStart=$APP_ROOT/current/venv/bin/uvicorn main_am:app --host 127.0.0.1 --port 8200
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# --- 7. AgentManager source ----------------------------------------------------
if [ -d "$REL_DST/service/agentmanager" ]; then
    install -d /opt/agent-manager
    cp -a "$REL_DST/service/agentmanager/." /opt/agent-manager/
fi

# --- 8. nginx site --------------------------------------------------------------
# TLS certs must exist before nginx is (re)loaded: staging generates a self-signed pair.
CRT="$ETC_ROOT/tls/llm-manager.crt"; KEY="$ETC_ROOT/tls/llm-manager.key"
if [ ! -s "$CRT" ] || [ ! -s "$KEY" ]; then
    if [ "$STAGING" = "1" ]; then
        openssl req -x509 -newkey rsa:2048 -keyout "$KEY" -out "$CRT" \
            -days 825 -nodes -subj "/CN=llm-manager-staging.marion-ia-usa.internal" >/dev/null 2>&1
        echo "==> staging self-signed TLS generated"
    else
        echo "WARN: $CRT/$KEY missing — operator must provision TLS before starting nginx"
    fi
fi
if [ -s "$CRT" ] && [ -s "$KEY" ]; then
    sed -e 's#server_name llm-manager.marion-ia-usa.internal 10.0.20.108;#server_name llm-manager.marion-ia-usa.internal 10.0.20.108;#' \
        "$REL_DST/ops/nginx/llm-manager" > /etc/nginx/sites-available/llm-manager
    ln -sf /etc/nginx/sites-available/llm-manager /etc/nginx/sites-enabled/llm-manager
    rm -f /etc/nginx/sites-enabled/default
    nginx -t >/dev/null 2>&1 && echo "==> nginx config validates" || { echo "FATAL: nginx -t failed"; exit 1; }
fi

# --- 9. First activation + enable ------------------------------------------------
if [ ! -e "$APP_ROOT/current" ]; then
    ln -s "$REL_DST" "$APP_ROOT/current"
    echo "==> activated release $REL_SHA (first bootstrap)"
fi

systemctl daemon-reload
systemctl enable llm-manager-web.service llm-manager-recovery.service \
    llm-manager-collector.service llm-manager-emporia.service litellm.service agent-manager.service >/dev/null 2>&1 || true

echo "==> bootstrap complete for release $REL_SHA"
echo "    NOTE: start services only after secrets + TLS + DB endpoints are provisioned."
