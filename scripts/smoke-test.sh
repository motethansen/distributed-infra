#!/usr/bin/env bash
# End-to-end check on ONE machine, no Tailscale, no real secrets, nothing outside a temp folder:
# starts an orchestrator and a worker on localhost, submits a task, waits for the result, cleans up.
#
#   scripts/smoke-test.sh            uses ./.venv (create it first: see docs/SETUP.md)
#   PYTHON_VENV=/path/.venv scripts/smoke-test.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PYTHON_VENV:-$ROOT/.venv}"
[[ -x "$VENV/bin/uvicorn" ]] || { echo "no $VENV/bin/uvicorn: run  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 2; }

OPORT="${SMOKE_ORCH_PORT:-18000}"; WPORT="${SMOKE_WORKER_PORT:-18001}"
TMP="$(mktemp -d)"; PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done; rm -rf "$TMP"; }
trap cleanup EXIT

SECRET="$(openssl rand -hex 16)"          # throwaway: exists only for this run
cat > "$TMP/.env" <<ENV
MACHINE_NAME=smoke
SECRET_KEY=$SECRET
ORCHESTRATOR_URL=http://127.0.0.1:$OPORT
ORCHESTRATOR_URLS=http://127.0.0.1:$OPORT
QUEUE_BACKEND=sqlite
QUEUE_DB_PATH=$TMP/queue.db
MACHINE_CAPABILITIES=run_script
WORKER_PORT=$WPORT
POLL_INTERVAL_SECONDS=1
ENV
chmod 600 "$TMP/.env"
cd "$ROOT"

MACHINE_ROLE=orchestrator scripts/run-with-env.sh --env-file "$TMP/.env" -- "$VENV/bin/uvicorn" orchestrator.main:app --host 127.0.0.1 --port "$OPORT" >"$TMP/orchestrator.log" 2>&1 & PIDS+=($!)
MACHINE_ROLE=worker       scripts/run-with-env.sh --env-file "$TMP/.env" -- "$VENV/bin/uvicorn" worker.main:app       --host 127.0.0.1 --port "$WPORT" >"$TMP/worker.log" 2>&1 & PIDS+=($!)

for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$OPORT/health" >/dev/null 2>&1 && break; sleep 0.5; done
curl -sf "http://127.0.0.1:$OPORT/health" >/dev/null || { echo "orchestrator did not start:"; tail -20 "$TMP/orchestrator.log"; exit 1; }
echo "ok   orchestrator is up"

TID="$(curl -sf -X POST "http://127.0.0.1:$OPORT/tasks" -H "x-secret-key: $SECRET" -H 'content-type: application/json' \
      -d '{"type":"run_script","payload":{"script":"echo smoke-test-ok"}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo "ok   task $TID queued"

for _ in $(seq 1 30); do
  body="$(curl -sf "http://127.0.0.1:$OPORT/tasks/$TID" -H "x-secret-key: $SECRET")"
  status="$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
  [[ "$status" == "done" || "$status" == "failed" || "$status" == "needs_human" ]] && break
  sleep 1
done
if [[ "$status" == "done" ]] && printf '%s' "$body" | grep -q smoke-test-ok; then
  echo "ok   a worker claimed the task and returned its output"
  echo "PASS"
else
  echo "FAIL: task ended '$status'"; tail -15 "$TMP/worker.log"; exit 1
fi
