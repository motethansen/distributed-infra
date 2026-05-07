#!/usr/bin/env bash
# Setup script for MacBook Pro (orchestrator)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Setting up orchestrator on MacBook Pro"

# Python venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r orchestrator/requirements.txt

# Create data dir for SQLite
mkdir -p data

# Symlink CLI
ln -sf "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/.venv/bin/python3"

echo ""
echo "==> Done. Next steps:"
echo "  1. cp .env.example .env  (and fill in SECRET_KEY)"
echo "  2. source .venv/bin/activate"
echo "  3. uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000"
echo "  4. python orchestrator/cli.py status"
