#!/usr/bin/env bash
# Uninstall mp3cut (macOS / Linux). Removes only what the installer added:
# the mp3cut package and (after confirmation) the marked PATH block in your shell files.
# Your MP3 files, other PATH entries and other settings are never touched.
set -euo pipefail
YES_FLAG=""; PIP_YES=""
case "${1:-}" in -y|--yes) YES_FLAG="--yes"; PIP_YES="-y" ;; esac

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3 python; do command -v "$c" >/dev/null 2>&1 && { PY="$(command -v "$c")"; break; }; done
fi
[ -n "$PY" ] || { echo "ERROR: python3 not found" >&2; exit 1; }

cd "$HOME"
if "$PY" -c 'import mp3cutter' >/dev/null 2>&1; then
  "$PY" -m mp3cutter.pathsetup remove $YES_FLAG
fi

ARGS=(uninstall $PIP_YES mp3cut)
if ! "$PY" -m pip "${ARGS[@]}" 2>/tmp/mp3cut-uninstall.$$; then
  if grep -qi "externally-managed-environment" /tmp/mp3cut-uninstall.$$; then
    "$PY" -m pip "${ARGS[@]}" --break-system-packages
  else
    cat /tmp/mp3cut-uninstall.$$ >&2
  fi
fi
rm -f /tmp/mp3cut-uninstall.$$
echo "mp3cut uninstalled."
