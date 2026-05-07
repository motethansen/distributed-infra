#!/usr/bin/env bash
# Quick connectivity test from MacBook — run after setup on all machines
set -euo pipefail

source "$(dirname "$0")/../.env" 2>/dev/null || true

MACBOOK_IP="100.97.176.37"
MACMINI_IP="100.76.214.54"
THINKPAD_IP="100.112.241.6"

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
