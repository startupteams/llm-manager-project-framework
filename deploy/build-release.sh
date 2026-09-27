#!/usr/bin/env bash
# build-release.sh — build an immutable LLM Manager release artifact from a Git worktree.
# Usage: build-release.sh [worktree-dir] [output-dir]
# Produces: llm-manager-<git-sha>.tar.gz + llm-manager-<git-sha>.tar.gz.sha256
# Excludes: secrets, venvs, logs, DB dumps/contents, caches, .git
set -euo pipefail

SRC_DIR="${1:-$(pwd)}"
OUT_DIR="${2:-$(pwd)/dist}"

GIT_SHA="$(git -C "$SRC_DIR" rev-parse HEAD)"
GIT_SHA_SHORT="$(git -C "$SRC_DIR" rev-parse --short=12 HEAD)"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "==> Building release for ${GIT_SHA} from ${SRC_DIR}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/llm-manager-${GIT_SHA_SHORT}"

mkdir -p "$STAGE"
# Copy tracked files only — this is the strongest guarantee that the artifact
# equals reviewed source (untracked local junk can never leak in).
git -C "$SRC_DIR" archive --format=tar HEAD | tar -x -C "$STAGE"

# Release manifest (non-secret build identity)
python3 - "$STAGE" "$GIT_SHA" "$BUILD_TIME" <<'PY'
import hashlib, json, os, sys
stage, sha, build_time = sys.argv[1], sys.argv[2], sys.argv[3]
files = {}
for root, dirs, names in os.walk(stage):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "venv")]
    for n in sorted(names):
        p = os.path.join(root, n)
        rel = os.path.relpath(p, stage)
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        files[rel] = h
manifest = {
    "git_sha": sha,
    "build_time": build_time,
    "python": "3.12",
    "files": files,
}
with open(os.path.join(stage, "release_manifest.json"), "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
print(f"manifest: {len(files)} files")
PY

# Secret scan gate: fail the build if obvious credentials slip into the artifact
if grep -rInE "(sk-[A-Za-z0-9]{20,}|PG_PW=[A-Za-z0-9]|BEGIN (RSA|OPENSSH) PRIVATE KEY)" "$STAGE" \
    --exclude-dir=.git --exclude=release_manifest.json >/dev/null 2>&1; then
    echo "FATAL: potential secret found in artifact (see matches above)"; exit 1
fi
# DSN-with-password check
if grep -rInE "postgresql(ql)?\+?://[^/\s:]+:[^@\s/]+@" "$STAGE" --exclude-dir=.git \
    --exclude=release_manifest.json --include="*.py" --include="*.yaml" --include="*.yml" \
    --include="*.sql" --include="*.conf" >/dev/null 2>&1; then
    echo "FATAL: DSN with password found in artifact"; exit 1
fi

mkdir -p "$OUT_DIR"
TARBALL="$OUT_DIR/llm-manager-${GIT_SHA_SHORT}.tar.gz"
tar -czf "$TARBALL" -C "$WORK" "llm-manager-${GIT_SHA_SHORT}"
(cd "$OUT_DIR" && sha256sum "$(basename "$TARBALL")" > "$(basename "$TARBALL").sha256")

echo "==> Built $TARBALL ($(du -h "$TARBALL" | cut -f1))"
