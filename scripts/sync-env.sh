#!/usr/bin/env bash
# Copies .env from the orchestrator to all worker machines via Tailscale / SSH.
# Run from the repo root: bash scripts/sync-env.sh
#
# Set these env vars to match your Tailscale IPs and remote usernames:
#   THINKPAD_IP=100.x.x.x MACMINI_IP=100.x.x.x bash scripts/sync-env.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

# Read IPs from environment — must be set or script will error
THINKPAD_IP="${THINKPAD_IP:?Set THINKPAD_IP to your ThinkPad Tailscale IP}"
MACMINI_IP="${MACMINI_IP:?Set MACMINI_IP to your Mac Mini Tailscale IP}"

# Detect remote username — default to local $USER unless overridden
THINKPAD_USER="${THINKPAD_USER:-$USER}"
MACMINI_USER="${MACMINI_USER:-$USER}"

# Remote path to drop the .env into
REMOTE_PATH="~/distributed-infra/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env not found at $ENV_FILE"
  echo "  Run: cp .env.example .env  and fill in the values first."
  exit 1
fi

scp_to() {
  local user=$1 ip=$2 label=$3
  echo -n "  → $label ($user@$ip) ... "
  if scp -q "$ENV_FILE" "${user}@${ip}:${REMOTE_PATH}"; then
    echo "done"
  else
    echo "FAILED (check SSH access and that ~/distributed-infra exists)"
  fi
}

echo ""
echo "==> Syncing .env to worker machines"
scp_to "$THINKPAD_USER" "$THINKPAD_IP" "ThinkPad (Ubuntu)"
scp_to "$MACMINI_USER"  "$MACMINI_IP"  "Mac Mini (iOS)"

echo ""
echo "Done. Workers will pick up the new .env on next restart."
echo ""
echo "Tip: override remote username if it differs from local:"
echo "  THINKPAD_IP=100.x.x.x MACMINI_IP=100.x.x.x THINKPAD_USER=yourname bash scripts/sync-env.sh"
