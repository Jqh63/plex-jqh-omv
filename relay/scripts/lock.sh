#!/usr/bin/env bash
# lock.sh — compile requirements-<venv>.in into requirements-<venv>.lock
# (exact versions + sha256 hashes), for the relay VM's Python, not the local one.
#
#   bash relay/scripts/lock.sh            # re-lock, keep current pins where possible
#   bash relay/scripts/lock.sh --upgrade  # move every pin to the latest release
#
# Runs on the deploying host (needs `uv`), never on the VM. The target is
# pinned here because it is load-bearing: a lock resolved for another Python
# or glibc may pick wheels the VM cannot install (Debian 12: CPython 3.11,
# glibc 2.36). Change these two with the VM's OS, in the same PR.
# The result must pass the grammar the VM enforces (venv-run.sh lint).
set -euo pipefail
PY_VERSION=3.11
PLATFORM=x86_64-manylinux_2_36
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
[ -x "$UV" ] || { echo "lock.sh: uv not found (set UV=/path/to/uv)" >&2; exit 1; }
upgrade=()
case "${1:-}" in "") ;; --upgrade) upgrade=(--upgrade) ;; *) echo "usage: lock.sh [--upgrade]" >&2; exit 64 ;; esac
cd "$HERE"
for v in wol-relay pock-sync; do
  "$UV" pip compile --quiet "${upgrade[@]}" --python-version "$PY_VERSION" \
    --python-platform "$PLATFORM" --generate-hashes \
    "requirements-$v.in" -o "requirements-$v.lock"
  bash "$HERE/venv-run.sh" lint "requirements-$v.lock" \
    || { echo "lock.sh: requirements-$v.lock fails the VM's lock grammar" >&2; exit 1; }
  echo "requirements-$v.lock: $(grep -cE '^[A-Za-z0-9].*==' "requirements-$v.lock") pins"
done
