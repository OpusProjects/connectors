#!/usr/bin/env bash
# Every inventory connector carries the shared helpers inline, so it runs as a
# single file anywhere. This keeps each copy identical to the reference in
# unified_connectors/inventory.py, and fails a connector that still imports
# the SDK instead (only gitops connectors may).
set -euo pipefail
ref=unified_connectors/inventory.py
block() { sed -n '/^# --- begin shared helpers/,/^# --- end shared helpers ---$/p' "$1"; }
want=$(block "$ref")
fail=0
for f in */*.py; do
  case "$f" in unified_connectors/*|*/tests/*) continue ;; esac
  if grep -q '^# --- begin shared helpers' "$f"; then
    if [ "$(block "$f")" != "$want" ]; then
      echo "::error file=$f::shared helpers differ from $ref; copy the block again"
      diff <(echo "$want") <(block "$f") || true
      fail=1
    fi
  elif grep -q 'from unified_connectors import' "$f" && ! grep -q 'run(' "$f"; then
    echo "::error file=$f::an inventory connector must carry the shared helpers, not import the SDK"
    fail=1
  fi
done
exit $fail
