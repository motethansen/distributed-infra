#!/usr/bin/env bash
# End-to-end check on ONE machine, no Tailscale, no real secrets, nothing outside a temp folder:
# starts an orchestrator and a worker on localhost, submits a task, waits for the result, cleans up.
#
#   scripts/smoke-test.sh            uses ./.venv (create it first: see docs/SETUP.md)
#   PYTHON_VENV=/path/.venv scripts/smoke-test.sh
#
# Ports are picked free at run time; the processes run under `env -i` with an explicit, local-only configuration.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PYTHON_VENV:-$ROOT/.venv}"
[[ -x "$VENV/bin/uvicorn" ]] || { echo "no $VENV/bin/uvicorn: run  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 2; }

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
OPORT="$(free_port)"; WPORT="$(free_port)"
while [[ "$WPORT" == "$OPORT" ]]; do WPORT="$(free_port)"; done
TMP="$(mktemp -d)"; PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done; rm -rf "$TMP"; }
trap cleanup EXIT

SECRET="$(openssl rand -hex 16)"          # throwaway: exists only for this run
RUN_ID="$(openssl rand -hex 4)"           # makes the machine names unique, so /health proves WHICH process answered
OURL="http://127.0.0.1:$OPORT"
cd "$ROOT"

# Both processes get a controlled environment (env -i): nothing from the caller's shell or ./.env can reach them, in
# particular no RQLITE_URL, real SECRET_KEY, queue path or orchestrator address. Only these variables exist.
isolated() {  # isolated MACHINE_NAME ROLE -- COMMAND...
  local name="$1" role="$2"; shift 2
  shift   # the --
  exec env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin:$VENV/bin" HOME="$TMP" TMPDIR="$TMP" \
    MACHINE_NAME="$name" MACHINE_ROLE="$role" SECRET_KEY="$SECRET" \
    QUEUE_BACKEND=sqlite QUEUE_DB_PATH="$TMP/queue.db" \
    ORCHESTRATOR_URL="$OURL" ORCHESTRATOR_URLS="$OURL" \
    MACHINE_CAPABILITIES=run_script WORKER_PORT="$WPORT" POLL_INTERVAL_SECONDS=1 \
    "$@"
}

ONAME="smoke-orch-$RUN_ID"; WNAME="smoke-worker-$RUN_ID"
isolated "$ONAME" orchestrator -- "$VENV/bin/uvicorn" orchestrator.main:app --host 127.0.0.1 --port "$OPORT" >"$TMP/orchestrator.log" 2>&1 & OPID=$!; PIDS+=("$OPID")
isolated "$WNAME" worker       -- "$VENV/bin/uvicorn" worker.main:app       --host 127.0.0.1 --port "$WPORT" >"$TMP/worker.log" 2>&1 & WPID=$!; PIDS+=("$WPID")

# Is this /health answer from OUR child? Alive, listening on the port we chose, and it names itself with this run's id.
verify() {  # verify LABEL PID PORT EXPECTED_MACHINE EXPECTED_ROLE
  local label="$1" pid="$2" port="$3" name="$4" role="$5" body
  kill -0 "$pid" 2>/dev/null || { echo "FAIL: $label (pid $pid) is not running:"; tail -20 "$TMP/$label.log"; return 1; }
  body="$(curl -sf "http://127.0.0.1:$port/health" -H "x-secret-key: $SECRET")" || { echo "FAIL: $label /health did not answer:"; tail -20 "$TMP/$label.log"; return 1; }
  printf '%s' "$body" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if (d.get("status"),d.get("machine"),d.get("role"))==("ok",sys.argv[1],sys.argv[2]) else 1)' "$name" "$role" \
    || { echo "FAIL: $label /health is not from this run's process (expected $name/$role): $body"; return 1; }
  if command -v lsof >/dev/null; then
    lsof -nP -a -p "$pid" -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || { echo "FAIL: pid $pid ($label) is not the listener on port $port"; return 1; }
  fi
  echo "ok   $label is up (pid $pid, port $port, answers as $name)"
}

for _ in $(seq 1 40); do
  curl -sf "$OURL/health" >/dev/null 2>&1 && curl -sf "http://127.0.0.1:$WPORT/health" -H "x-secret-key: $SECRET" >/dev/null 2>&1 && break
  kill -0 "$OPID" 2>/dev/null && kill -0 "$WPID" 2>/dev/null || break
  sleep 0.5
done
verify orchestrator "$OPID" "$OPORT" "$ONAME" orchestrator || exit 1
verify worker       "$WPID" "$WPORT" "$WNAME" worker       || exit 1

TID="$(curl -sf -X POST "$OURL/tasks" -H "x-secret-key: $SECRET" -H 'content-type: application/json' \
      -d '{"type":"run_script","payload":{"script":"echo smoke-test-ok"}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo "ok   task $TID queued"

for _ in $(seq 1 30); do
  body="$(curl -sf "$OURL/tasks/$TID" -H "x-secret-key: $SECRET")"
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
