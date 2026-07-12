#!/usr/bin/env bash
# Promote the standby to orchestrator after the primary (mac-mini) is lost.
#
# WHY THIS EXISTS
#   Phase 1 failover is MANUAL by design (see docs/rqlite-failover.md — automatic
#   failover is Phase 2 / rqlite). This script is the runbook-as-code for the data
#   half: reconstruct the queue from the Litestream replica on the standby and,
#   if an orchestrator checkout exists here, start it against the restored DB.
#   The final client cutover (repointing ORCHESTRATOR_URL) is printed, not
#   automated, so a human confirms the primary really is down (no split-brain).
#
#   Run this ON THE STANDBY (thinkpad).
#
# CONFIG (env, with defaults)
#   ORCH_DIR      orchestrator checkout on the standby (default: ~/Projects/distributed-infra)
#   QUEUE_DB      where the orchestrator expects the DB (default: $ORCH_DIR/data/queue.db)
#   REPLICA_DIR   received replica dir                (default: ~/litestream-replicas/queue)
set -euo pipefail

ORCH_DIR="${ORCH_DIR:-$HOME/Projects/distributed-infra}"
QUEUE_DB="${QUEUE_DB:-$ORCH_DIR/data/queue.db}"
REPLICA_DIR="${REPLICA_DIR:-$HOME/litestream-replicas/queue}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "════════════════════════════════════════════════════════════════"
echo " FAILOVER PROMOTE — standby: $(hostname)"
echo "════════════════════════════════════════════════════════════════"
echo "⚠  Confirm the primary (mac-mini) is REALLY down before continuing."
echo "   Two live orchestrators on one queue = split-brain. Ctrl-C to abort."
echo

# 1) Restore the latest queue from the replica into the orchestrator's DB path.
mkdir -p "$(dirname "$QUEUE_DB")"
if [ -f "$QUEUE_DB" ]; then
  cp "$QUEUE_DB" "$QUEUE_DB.pre-promote.$(date +%s)"
  echo "• backed up existing $QUEUE_DB"
fi
REPLICA_DIR="$REPLICA_DIR" "$HERE/litestream-restore.sh" "$QUEUE_DB"

# 2) Start the orchestrator here, if this box has the checkout + venv.
if [ -f "$ORCH_DIR/.venv/bin/uvicorn" ] && [ -f "$ORCH_DIR/orchestrator/main.py" ]; then
  echo "• starting orchestrator on $(hostname) against restored DB…"
  ( cd "$ORCH_DIR" && QUEUE_DB_PATH="$QUEUE_DB" nohup .venv/bin/uvicorn orchestrator.main:app \
      --host 0.0.0.0 --port 8000 >/tmp/orchestrator.log 2>&1 & )
  sleep 3
  curl -fsS "http://localhost:8000/health" && echo "  ✓ orchestrator answering on :8000" || echo "  ✗ not answering — check /tmp/orchestrator.log"
else
  echo "• no orchestrator checkout/venv at $ORCH_DIR — restore done, start it manually."
fi

# 3) Client cutover (manual — repoint everything at the new orchestrator).
cat <<EOF

NEXT — cut clients over to this box:
  • Repoint the Tailscale name 'orchestrator' (or ORCHESTRATOR_URL) at $(hostname)'s IP.
  • Restart / re-env: WhatsApp bridge, all workers, the 'da' CLI.
  • DO NOT start Litestream 'replicate' here yet — first decide the new primary,
    then flip replication direction so you don't overwrite the good replica.

FAILBACK (when mac-mini returns): make mac-mini restore from THIS box's replica,
  or copy the current DB back, before re-pointing clients. Never run two
  orchestrators on the same logical queue at once.
EOF
