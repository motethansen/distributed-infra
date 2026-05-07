#!/usr/bin/env bash
# Setup script for ThinkPad X13 (Ubuntu) — Android + backend worker
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Setting up worker on ThinkPad (Ubuntu)"

# Ensure python3-venv is available
if ! python3 -m venv --help &>/dev/null; then
  sudo apt-get update && sudo apt-get install -y python3-venv python3-pip
fi

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r worker/requirements.txt

echo ""
echo "==> Done. Next steps:"
echo "  1. cp .env.example .env  (set MACHINE_NAME, MACHINE_ROLE=worker, MACHINE_CAPABILITIES)"
echo "     MACHINE_CAPABILITIES=android_build,gradle,git_pull,run_script,python_backend,node_backend"
echo "  2. source .venv/bin/activate"
echo "  3. uvicorn worker.main:app --host 0.0.0.0 --port 8001"
echo ""
echo "  To run as a systemd service: see scripts/worker.service"
