#!/usr/bin/env bash
# Restore the orchestrator queue DB from the Litestream replica.
#
# WHY THIS EXISTS
#   Phase 1 failover (see docs/litestream-runbook.md, docs/rqlite-failover.md).
#   The primary (mac-mini) streams the queue to the standby (thinkpad) via SFTP.
#   When the primary is lost, run this on the STANDBY to reconstruct a current
#   queue DB from the locally-received replica files — the data half of a promote.
#
#   It restores to a temp path and prints the task count so you can eyeball it
#   before swapping it into place, rather than clobbering anything.
#
# USAGE
#   scripts/litestream-restore.sh [OUTPUT_DB]
#     OUTPUT_DB   where to write the restored DB (default: /tmp/restored-queue.db)
#
# CONFIG (env, with defaults)
#   REPLICA_DIR   local dir holding the received replica (default: ~/litestream-replicas/queue)
#   LITESTREAM    path to the litestream binary       (default: ~/.local/bin/litestream)
set -euo pipefail

OUTPUT_DB="${1:-/tmp/restored-queue.db}"
REPLICA_DIR="${REPLICA_DIR:-$HOME/litestream-replicas/queue}"
LITESTREAM="${LITESTREAM:-$HOME/.local/bin/litestream}"

[ -x "$LITESTREAM" ] || { echo "✗ litestream not found at $LITESTREAM" >&2; exit 1; }
[ -d "$REPLICA_DIR/generations" ] || { echo "✗ no replica generations under $REPLICA_DIR" >&2; exit 1; }

CFG="$(mktemp)"
trap 'rm -f "$CFG"' EXIT
cat > "$CFG" <<YAML
dbs:
  - path: ${OUTPUT_DB}
    replicas:
      - type: file
        path: ${REPLICA_DIR}
YAML

echo "▶ restoring ${REPLICA_DIR} → ${OUTPUT_DB}"
rm -f "$OUTPUT_DB"
"$LITESTREAM" restore -config "$CFG" "$OUTPUT_DB"

# Report so a human can sanity-check before swapping it in.
if command -v sqlite3 >/dev/null 2>&1; then
  COUNT="$(sqlite3 "$OUTPUT_DB" 'SELECT count(*) FROM tasks;')"
else
  COUNT="$(python3 -c "import sqlite3;print(sqlite3.connect('${OUTPUT_DB}').execute('select count(*) from tasks').fetchone()[0])")"
fi
echo "✓ restored ${COUNT} tasks to ${OUTPUT_DB}"
echo "  next: point the orchestrator's QUEUE_DB_PATH at this file and start it here"
echo "  (see scripts/failover-promote.sh / docs/litestream-runbook.md)"
