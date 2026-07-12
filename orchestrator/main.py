"""Orchestrator queue server — runs on MacBook Pro."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, FastAPI, HTTPException, Header, Query
from fastapi.responses import Response

from shared.models import ClaimRequest, Task, TaskCreate, TaskStatus, TaskType, TaskUpdate
from orchestrator import db

SECRET_KEY = os.getenv("SECRET_KEY", "")
MACHINE_NAME = os.getenv("MACHINE_NAME", "orchestrator")

# Workload placement (#5b): agent-style tasks prefer the primary box (Mac Mini) by
# default and overflow to other capable workers after the grace window. Build/dev
# tasks are pinned by capability and are left with NO preference, so the only
# capable worker claims them immediately (no grace delay).
DEFAULT_PREFERRED_MACHINE = os.getenv("DEFAULT_PREFERRED_MACHINE", "mac-mini")
PREFER_PRIMARY_TYPES = {
    TaskType.agent_run, TaskType.assistant_run, TaskType.assistant_query,
    TaskType.weather, TaskType.write_article, TaskType.write_post, TaskType.code_review,
    TaskType.find, TaskType.plan, TaskType.market_brief, TaskType.morning_brief,
}

_MACHINES_CONFIG = Path(__file__).parent.parent / "config" / "machines.yaml"
_ONLINE_WINDOW_SECS = 30  # worker process is "active" if it polled within this window
_last_seen: dict[str, float] = {}  # worker_name → unix ts of last claim poll


# ── Tailscale liveness ───────────────────────────────────────────────────────
# A machine's online/offline is derived from Tailscale's own view of the tailnet
# (`tailscale status --json`), not from whether its worker happened to poll the
# queue recently. This is the machine-reachability baseline; claim-poll recency is
# kept separately as `worker_active` (process liveness). Falls back to the
# claim-poll heuristic only when the Tailscale CLI is unavailable.
_TS_TTL = 5.0  # seconds to cache a `tailscale status` probe
_TS_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}
_TS_BIN_CANDIDATES = (
    "tailscale", "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def _tailscale_bin() -> str | None:
    for c in _TS_BIN_CANDIDATES:
        if "/" in c:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def _parse_last_seen(s: str | None) -> datetime | None:
    # Tailscale reports a zero timestamp (0001-01-01…) for currently-online peers.
    if not s or s.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tailscale_liveness() -> dict[str, dict]:
    """Map tailscale_ip → {online: bool, last_seen: datetime|None}, cached briefly.
    Returns an empty dict when the CLI is missing or errors, so callers fall back
    to the claim-poll heuristic."""
    now = time.time()
    if now - _TS_CACHE["ts"] < _TS_TTL:
        return _TS_CACHE["data"]
    out: dict[str, dict] = {}
    binp = _tailscale_bin()
    if binp:
        try:
            proc = subprocess.run(
                [binp, "status", "--json"],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                data = json.loads(proc.stdout or "{}")
                nodes = []
                if data.get("Self"):
                    nodes.append(data["Self"])
                nodes.extend((data.get("Peer") or {}).values())
                for n in nodes:
                    info = {"online": bool(n.get("Online")),
                            "last_seen": _parse_last_seen(n.get("LastSeen"))}
                    for ip in (n.get("TailscaleIPs") or []):
                        out[ip] = info
        except (subprocess.SubprocessError, ValueError, OSError):
            out = {}
    _TS_CACHE["ts"] = now
    _TS_CACHE["data"] = out
    return out


app = FastAPI(title="Distributed Infra Queue", version="0.1.0")


def _check_auth(x_secret_key: str = "") -> None:
    if SECRET_KEY and x_secret_key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid secret key")


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "machine": MACHINE_NAME, "role": "orchestrator"}


# ── Task CRUD ────────────────────────────────────────────────────────────────

@app.post("/tasks", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    payload = dict(body.payload or {})
    # Default an agent-style task to prefer the primary box, unless the caller
    # already pinned it (_target_machine) or set its own preference.
    if (body.type in PREFER_PRIMARY_TYPES
            and DEFAULT_PREFERRED_MACHINE
            and not payload.get("_target_machine")
            and not payload.get("_preferred_machine")):
        payload["_preferred_machine"] = DEFAULT_PREFERRED_MACHINE
    task = Task(
        type=body.type,
        priority=body.priority,
        payload=payload,
        notes=body.notes,
        created_by=MACHINE_NAME,
    )
    return await db.insert_task(task)


@app.get("/tasks", response_model=list[Task])
async def list_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    x_secret_key: str = Header(default=""),
):
    _check_auth(x_secret_key)
    return await db.list_tasks(status=status, limit=limit)


@app.get("/tasks/needs-human", response_model=list[Task])
async def tasks_needing_human(x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    return await db.list_tasks(status=TaskStatus.needs_human)


@app.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: str, x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.patch("/tasks/{task_id}", response_model=Task)
async def update_task(task_id: str, body: TaskUpdate, x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    fields: dict[str, Any] = body.model_dump(exclude_none=True)
    task = await db.update_task(task_id, fields)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ── Worker queue operations ──────────────────────────────────────────────────

@app.post("/tasks/claim", response_model=Task | None)
async def claim_task(body: ClaimRequest, x_secret_key: str = Header(default="")):
    """Worker calls this to atomically claim the next available task."""
    _check_auth(x_secret_key)
    _last_seen[body.worker_name] = time.time()
    task = await db.claim_next_task(body.worker_name, body.capabilities)
    if not task:
        # 204 No Content MUST NOT carry a body; a bodyless Response is correct
        # across Starlette versions (JSONResponse(content=None) renders b"null",
        # which trips uvicorn's "content longer than Content-Length" on a 204).
        return Response(status_code=204)
    return task


# ── Machine roster ───────────────────────────────────────────────────────────

def _load_machines() -> dict:
    with open(_MACHINES_CONFIG) as f:
        return yaml.safe_load(f).get("machines", {})


@app.get("/machines")
async def list_machines(x_secret_key: str = Header(default="")):
    """Roster from machines.yaml + liveness derived from worker claim polls.

    The orchestrator row is always online (you can only reach this endpoint
    when it is). Workers are online if they polled within _ONLINE_WINDOW_SECS.
    """
    _check_auth(x_secret_key)
    machines = _load_machines()
    now = time.time()
    ts = _tailscale_liveness()  # tailscale_ip → {online, last_seen}

    out = []
    for name, cfg in machines.items():
        # Worker-process liveness: freshest claim poll under this name or an alias.
        last_ts = _last_seen.get(name)
        for alias in cfg.get("aliases", []) or []:
            alt = _last_seen.get(alias)
            if alt and (last_ts is None or alt > last_ts):
                last_ts = alt
        worker_active = last_ts is not None and (now - last_ts) < _ONLINE_WINDOW_SECS

        role = cfg.get("role", "worker")
        ip = cfg.get("tailscale_ip")
        tsinfo = ts.get(ip) if ip else None

        # Machine online = Tailscale baseline. Fall back to the claim-poll
        # heuristic only when Tailscale is unavailable.
        if tsinfo is not None:
            online = tsinfo["online"]
        else:
            online = True if role == "orchestrator" else worker_active

        # last_seen age: Tailscale's LastSeen when the machine is offline,
        # else the worker's last claim-poll age.
        last_seen_ago = None
        if tsinfo is not None and not tsinfo["online"] and tsinfo["last_seen"]:
            last_seen_ago = int((datetime.now(timezone.utc) - tsinfo["last_seen"]).total_seconds())
        elif last_ts:
            last_seen_ago = int(now - last_ts)

        out.append({
            "name": name,
            "role": role,
            "tailscale_ip": ip,
            "capabilities": cfg.get("capabilities", []) or [],
            "agents": cfg.get("agents", []) or [],
            "aliases": cfg.get("aliases", []) or [],
            "online": online,                # machine reachable on the tailnet
            "tailscale_online": (tsinfo["online"] if tsinfo is not None else None),
            "worker_active": worker_active,  # worker process polled within the window
            "last_seen_ago_secs": last_seen_ago,
        })
    return out


@app.post("/tasks/{task_id}/complete", response_model=Task)
async def complete_task(task_id: str, result: dict = Body(default_factory=dict), x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    task = await db.update_task(task_id, {"status": TaskStatus.done, "result": result})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/tasks/{task_id}/fail", response_model=Task)
async def fail_task(task_id: str, result: dict = Body(default_factory=dict), x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    task = await db.update_task(task_id, {"status": TaskStatus.failed, "result": result})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/tasks/{task_id}/needs-human", response_model=Task)
async def escalate_task(
    task_id: str,
    notes: str = "",
    action: str = "",
    x_secret_key: str = Header(default=""),
):
    """Worker escalates a task that requires human decision."""
    _check_auth(x_secret_key)

    # Combine notes + action into the stored notes field so it survives without
    # a schema change.  Format: "<notes> | ACTION: <action>"
    full_notes = notes
    if action:
        full_notes = f"{notes} | ACTION: {action}" if notes else f"ACTION: {action}"

    task = await db.update_task(task_id, {"status": TaskStatus.needs_human, "notes": full_notes})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # ── macOS notification on the orchestrator machine ───────────────────────
    _notify(
        title="⚠️ Task needs your attention",
        subtitle=f"{task_id[:8]}  ·  {(task.payload or {}).get('type', task.type)}",
        message=action or notes or "A task requires human action — run: da › review",
    )

    return task


def _notify(title: str, subtitle: str, message: str) -> None:
    """Fire a macOS notification. Silently ignored on non-macOS hosts."""
    try:
        # Escape double quotes for AppleScript
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')[:200]

        script = (
            f'display notification "{esc(message)}" '
            f'with title "{esc(title)}" '
            f'subtitle "{esc(subtitle)}"'
        )
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass  # not macOS
