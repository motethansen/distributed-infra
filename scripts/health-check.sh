#!/usr/bin/env bash
# Quick connectivity test from the orchestrator — run after setup on all machines.
# Reads IPs from .env (ORCHESTRATOR_URL, MACMINI_IP, THINKPAD_IP) or from env vars.
set -euo pipefail

source "$(dirname "$0")/../.env" 2>/dev/null || true

# Read IPs from environment — set these to your Tailscale IPs
MACBOOK_IP="${MACBOOK_IP:?Set MACBOOK_IP to your MacBook Tailscale IP}"
MACMINI_IP="${MACMINI_IP:?Set MACMINI_IP to your Mac Mini Tailscale IP}"
THINKPAD_IP="${THINKPAD_IP:?Set THINKPAD_IP to your ThinkPad Tailscale IP}"

check() {
  local name=$1 url=$2
  if curl -sf -H "x-secret-key: ${SECRET_KEY:-}" "$url/health" > /dev/null 2>&1; then
    echo "  ✓ $name  ($url)"
  else
    echo "  ✗ $name  ($url)  — not reachable"
  fi
}

echo ""
echo "==> Checking orchestrator (MacBook)"
check "macbook-pro" "http://${MACBOOK_IP}:8000"

echo ""
echo "==> Checking workers"
check "mac-mini"  "http://${MACMINI_IP}:8001"
check "thinkpad"  "http://${THINKPAD_IP}:8001"

echo ""
echo "Tip: pass IPs inline if not set in .env:"
echo "  MACBOOK_IP=100.x.x.x MACMINI_IP=100.x.x.x THINKPAD_IP=100.x.x.x bash scripts/health-check.sh"
echo ""
