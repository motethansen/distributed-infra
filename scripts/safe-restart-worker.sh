#!/usr/bin/env bash
# Restart this machine's worker safely — refuses while any task is in_progress
# (because killing the worker mid-task leaves an orphan: orchestrator still
# shows in_progress, no one is actually working on it, recovery is manual).
#
# Lesson from Sprint W1 (orchestrator task a19bb8c0, 2026-05-13): a routine
# `launchctl kickstart -k` of the worker to deploy a config change killed
# claude mid-execution. ~840 lines of partial work were stranded; orphan
# task had to be patched to failed and remainder re-dispatched.
#
# Usage:
#   scripts/safe-restart-worker.sh                    # safe — checks first
#   scripts/safe-restart-worker.sh --force            # bypass check (for emergencies)
#   scripts/safe-restart-worker.sh --orchestrator URL # override default orchestrator URL
#
# Env (read from .env if present):
#   ORCHESTRATOR_URL  — default http://100.97.176.37:8000
#   SECRET_KEY        — required to query queue
#   MACHINE_NAME      — restricts the in_progress check to THIS machine's tasks

set -euo pipefail

cd "$(dirname "$0")/.."

# Load .env if present
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; source .env; set +a
fi

FORCE=0
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://100.97.176.37:8000}"

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --orchestrator) ORCHESTRATOR_URL="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "[safe-restart] unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "${SECRET_KEY:-}" ]; then
  echo "[safe-restart] SECRET_KEY not set (need it to query queue). Refusing." >&2
  exit 1
fi

log() { printf '[safe-restart] %s\n' "$*"; }

# Pre-check: any in_progress tasks claimed by THIS machine?
log "checking orchestrator at $ORCHESTRATOR_URL for in-progress tasks..."

if ! tasks_json=$(curl -s --max-time 8 \
                       -H "x-secret-key: $SECRET_KEY" \
                       "$ORCHESTRATOR_URL/tasks?status=in_progress&limit=100" 2>&1); then
  log "WARNING: could not reach orchestrator. ($tasks_json)"
  if [ "$FORCE" != 1 ]; then
    log "refusing to restart blind. Pass --force if the orchestrator is genuinely down."
    exit 1
  fi
  log "--force passed; proceeding without check"
else
  # Filter to this machine if MACHINE_NAME is set; otherwise count globally
  if [ -n "${MACHINE_NAME:-}" ]; then
    in_progress=$(echo "$tasks_json" | jq --arg m "$MACHINE_NAME" \
      '[.[] | select(.assigned_to == $m)] | length')
    scope="for machine '$MACHINE_NAME'"
  else
    in_progress=$(echo "$tasks_json" | jq 'length')
    scope="across all machines (no MACHINE_NAME set)"
  fi

  if [ "$in_progress" -gt 0 ]; then
    log "FOUND $in_progress in-progress task(s) $scope:"
    echo "$tasks_json" | jq -r --arg m "${MACHINE_NAME:-}" '
      .[] | select($m == "" or .assigned_to == $m) |
      "  - \(.id[0:8])  \(.type)  assigned=\(.assigned_to // "-")  updated=\(.updated_at)"'
    if [ "$FORCE" != 1 ]; then
      log ""
      log "refusing restart — would orphan these tasks."
      log ""
      log "options:"
      log "  1. wait for them to finish, then re-run this script"
      log "  2. mark them failed first (PATCH /tasks/<id> {\"status\":\"failed\"}), then restart"
      log "  3. --force (overrides; you are responsible for recovery)"
      exit 1
    fi
    log "--force passed; proceeding to restart anyway. Document the orphaning."
  else
    log "no in-progress tasks $scope. Safe to restart."
  fi
fi

# Detect platform and restart accordingly
if [ "$(uname)" = "Darwin" ]; then
  if launchctl print "gui/$(id -u)/com.techstartups.worker" >/dev/null 2>&1; then
    log "restarting com.techstartups.worker via launchctl"
    launchctl kickstart -k "gui/$(id -u)/com.techstartups.worker"
  else
    log "ERROR: launchctl service com.techstartups.worker not loaded" >&2
    exit 1
  fi
elif [ "$(uname)" = "Linux" ]; then
  if systemctl --user status infra-worker >/dev/null 2>&1; then
    log "restarting infra-worker via systemd --user"
    systemctl --user restart infra-worker
  elif systemctl status infra-worker >/dev/null 2>&1; then
    log "restarting infra-worker via systemd (system)"
    sudo systemctl restart infra-worker
  else
    log "ERROR: infra-worker systemd unit not found (user or system)" >&2
    exit 1
  fi
else
  log "ERROR: unsupported platform $(uname)" >&2
  exit 1
fi

# Wipe stale .pyc — Python kept old bytecode after the W1 dispatch on May 12,
# causing the worker to import the *old* claude_agent.py despite the file
# being updated. Wiping pycache forces clean imports.
log "wiping stale __pycache__ to ensure fresh imports"
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

sleep 2
log "done. Verify with:"
log "  curl -H 'x-secret-key: \$SECRET_KEY' '$ORCHESTRATOR_URL/machines'"
