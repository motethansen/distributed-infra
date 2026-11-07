#!/usr/bin/env python3
"""Docker engine watchdog for the mac-mini (OrbStack).

Runs once per launchd tick (StartInterval 60). It exists because the engine hung on
2026-11-06 and nothing noticed for almost two hours.

Each tick it:
  1. asks the engine for its version with a hard timeout (a hung engine answers nothing);
  2. if the engine answers, starts any expected container that is not running;
  3. if the engine stays silent for FAIL_LIMIT ticks in a row, tries `orbctl start`
     (at most once per COOLDOWN_S). It never resets or deletes engine data;
  4. reports one status to Vantage (up / degraded / down) so the board shows it and the
     entity goes stale when this job itself stops running.

Settings come from the environment (no secrets in this file or in the plist):
  WATCHDOG_CONTAINERS  comma list of containers that must be running
  VANTAGE_URL, VANTAGE_INGEST_KEY, WATCHDOG_COMPONENT   (reporting is skipped if URL is unset)
  WATCHDOG_STATE       path of the small JSON state file
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

FAIL_LIMIT = 3          # silent ticks before we try to start the engine
COOLDOWN_S = 600        # minimum gap between engine start attempts
PROBE_TIMEOUT_S = 15
START_TIMEOUT_S = 90


def run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run a command with a hard timeout. Returns (rc, output); rc 124 means timed out."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def tick(state: dict, containers: list[str], now: float, runner=run,
         docker: str = "docker", orbctl: str = "orbctl") -> tuple[str, list[str]]:
    """One check. Mutates `state`, returns (status, errors)."""
    rc, out = runner([docker, "version", "--format", "{{.Server.Version}}"], PROBE_TIMEOUT_S)
    if rc == 0 and out:
        state["fails"] = 0
        errors: list[str] = []
        for name in containers:
            rc, out = runner([docker, "inspect", "-f", "{{.State.Running}}", name], PROBE_TIMEOUT_S)
            if rc == 0 and out == "true":
                continue
            rc, out = runner([docker, "start", name], PROBE_TIMEOUT_S)
            errors.append(f"{name} was not running; start {'ok' if rc == 0 else 'FAILED: ' + out[:120]}")
        return ("degraded" if errors else "up"), errors

    state["fails"] = state.get("fails", 0) + 1
    errors = [f"docker engine not answering ({out[:120]}); {state['fails']} checks in a row"]
    if state["fails"] >= FAIL_LIMIT and now - state.get("last_start", 0) >= COOLDOWN_S:
        state["last_start"] = now
        rc, out = runner([orbctl, "start"], START_TIMEOUT_S)
        errors.append(f"orbctl start: {'ok' if rc == 0 else 'FAILED rc=' + str(rc) + ' ' + out[:120]}")
    return "down", errors


def report(url: str, key: str, component: str, status: str, errors: list[str]) -> bool:
    body = json.dumps({"component": component, "status": status, "errors": errors[:20],
                       "checks": {"engine": "up" if status != "down" else "down"},
                       "ts": int(time.time())}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["x-vantage-key"] = key
    req = urllib.request.Request(url.rstrip("/") + "/api/ingest", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError):
        return False


def main() -> int:
    containers = [c.strip() for c in os.environ.get("WATCHDOG_CONTAINERS", "").split(",") if c.strip()]
    path = os.environ.get("WATCHDOG_STATE", os.path.expanduser("~/.docker-watchdog.json"))
    state = load_state(path)
    status, errors = tick(state, containers, time.time())
    save_state(path, state)
    print(f"{time.strftime('%F %T')} {status} {'; '.join(errors)}", flush=True)
    url = os.environ.get("VANTAGE_URL", "")
    if url:
        report(url, os.environ.get("VANTAGE_INGEST_KEY", ""),
               os.environ.get("WATCHDOG_COMPONENT", "mm-docker-engine"), status, errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
