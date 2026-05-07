#!/usr/bin/env bash
# Setup script for Mac Mini — iOS / Xcode worker
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Setting up worker on Mac Mini (iOS)"

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r worker/requirements.txt

echo ""
echo "==> Done. Next steps:"
echo "  1. cp .env.example .env  (set MACHINE_NAME=mac-mini, MACHINE_ROLE=worker)"
echo "     MACHINE_CAPABILITIES=ios_build,xcode,swift,testflight,git_pull,run_script"
echo "  2. source .venv/bin/activate"
echo "  3. uvicorn worker.main:app --host 0.0.0.0 --port 8001"
echo ""
echo "  Make sure Xcode command-line tools are installed:"
echo "    xcode-select --install"
