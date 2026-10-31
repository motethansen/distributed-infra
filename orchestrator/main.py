"""Orchestrator queue server — runs on MacBook Pro."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, FastAPI, HTTPException, Header, Query
from fastapi.responses import Response

from agents import roles as scrum_roles
from orchestrator.llm_choices import build_llm_choices
from shared import scrum, scrum_digest, tracking
from shared.models import ClaimRequest, Task, TaskCreate, TaskStatus, TaskType, TaskUpdate

# Queue backend selector (#20 Phase 2). Default = the single-node aiosqlite file
# (orchestrator.db). QUEUE_BACKEND=rqlite swaps in the Raft-cluster backend
# (orchestrator.db_rqlite, same signatures) so the queue survives any one node.
if os.getenv("QUEUE_BACKEND", "sqlite").lower() == "rqlite":
    from orchestrator import db_rqlite as db
else:
    from orchestrator import db

log = logging.getLogger(__name__)

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
_lan_ips: dict[str, dict[str, str]] = {}  # worker_name → {iface: ip}, as last reported
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
                self_ips = set((data.get("Self") or {}).get("TailscaleIPs") or [])
                for n in nodes:
                    cur = n.get("CurAddr") or ""
                    info = {"online": bool(n.get("Online")),
                            "last_seen": _parse_last_seen(n.get("LastSeen")),
                            # direct = peer-to-peer (on the LAN this is a 192.168.x address); a
                            # relay between two machines in the same house is itself the alarm.
                            # (this machine's own entry has no path; its `Relay` is just its home DERP region)
                            "path": (None if set(n.get("TailscaleIPs") or []) & self_ips
                                     else "direct" if cur else (f"relay:{n['Relay']}" if n.get("Relay") else None)),
                            "cur_addr": cur or None}
                    for ip in (n.get("TailscaleIPs") or []):
                        out[ip] = info
        except (subprocess.SubprocessError, ValueError, OSError):
            out = {}
    _TS_CACHE["ts"] = now
    _TS_CACHE["data"] = out
    return out


app = FastAPI(title="Distributed Infra Queue", version="0.1.0")


# A task claimed/in_progress with no update for this long has lost its worker (the
# worker died, restarted, or could not report). It moves to needs_human, with its
# result and notes intact, rather than sitting in the queue forever. Must exceed the
# longest legitimate task (builds run well under an hour).
STALE_TASK_SECS = int(os.getenv("STALE_TASK_SECS", str(6 * 3600)))
_REAP_EVERY_SECS = 600


async def reap_once() -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(seconds=STALE_TASK_SECS)).isoformat()
    note = (f"STALE: no update for over {STALE_TASK_SECS // 3600}h while claimed/in_progress — "
            f"worker lost or could not report; reaped {datetime.utcnow().isoformat(timespec='seconds')}Z")
    return await db.reap_stale(cutoff, note)


async def _reaper_loop() -> None:
    while True:
        try:
            for t in await reap_once():
                log.warning("reaped stale task %s (%s, was on %s)", str(t["id"])[:8], t["type"], t.get("assigned_to"))
        except Exception as exc:  # noqa: BLE001 — never let the reaper die
            log.warning("reaper: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(_REAP_EVERY_SECS)


@app.on_event("startup")
async def _init_backend() -> None:
    # rqlite backend needs a one-time schema create; the aiosqlite backend
    # ensures its schema per-call and has no ensure_schema (skipped).
    init = getattr(db, "ensure_schema", None)
    if init:
        await init()
    asyncio.create_task(_reaper_loop())


def _check_auth(x_secret_key: str = "") -> None:
    if SECRET_KEY and x_secret_key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid secret key")


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Readiness, not just 'the process answers': the orchestrator is useless without
    its queue, so a failing queue read is a 503 (callers such as da.py and Vantage
    then move to the other orchestrator instead of trusting this one)."""
    try:
        await asyncio.wait_for(db.list_tasks(limit=1), timeout=5)
        queue = "ok"
    except Exception as exc:  # noqa: BLE001 — any failure means the queue is unusable
        return Response(
            content=json.dumps({"status": "degraded", "machine": MACHINE_NAME, "role": "orchestrator",
                                "queue": f"error: {type(exc).__name__}: {exc}"[:300]}),
            status_code=503, media_type="application/json")
    return {"status": "ok", "machine": MACHINE_NAME, "role": "orchestrator", "queue": queue}


@app.post("/workers/heartbeat", status_code=204)
async def worker_heartbeat(body: dict = Body(...), x_secret_key: str = Header(default="")):
    """A worker announcing it is alive without claiming (it polls ONE orchestrator, but
    liveness is kept per orchestrator process, and a busy worker does not claim)."""
    _check_auth(x_secret_key)
    name = str(body.get("worker_name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="worker_name required")
    _last_seen[name] = time.time()
    ips = body.get("lan_ips")
    if isinstance(ips, dict):
        _lan_ips[name] = {str(k)[:32]: str(v)[:45] for k, v in list(ips.items())[:8]}
    return Response(status_code=204)


# ── Task CRUD ────────────────────────────────────────────────────────────────

async def _month_opus_cost() -> float:
    """This calendar month's Opus spend (the Claude CLI's cost estimate), from the task rows."""
    start = datetime.utcnow().strftime("%Y-%m-01")
    rows = await db.usage_rows(since=start)
    return sum(r.get("cost_usd") or 0.0 for r in rows if scrum_roles.is_opus(r.get("model")))


async def _apply_role(payload: dict) -> None:
    """Resolve payload.role (config/roles.yaml) into agent/model and the Opus grant.

    `_allow_opus` is policy, never input: whatever the caller sent is discarded first. An explicit
    payload.agent/model still wins over the role's choice, as with task-class routing."""
    payload.pop("_allow_opus", None)
    blocked = scrum_roles.isolated_violation(payload)     # every task, with or without a role
    if blocked:
        raise HTTPException(status_code=422, detail=blocked)
    role = payload.get("role")
    if not role:
        return
    try:
        month = await _month_opus_cost() if scrum_roles.is_opus(
            ((scrum_roles.load_policy().get("roles") or {}).get(str(role).strip().lower()) or {}).get("model")) else 0.0
        r = scrum_roles.resolve(str(role), project=payload.get("project"),
                                sensitivity=payload.get("sensitivity"), month_opus_cost=month,
                                prompt=str(payload.get("prompt") or ""), approved=payload.get("approved") is True)
    except scrum_roles.RoleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    payload["role"] = str(role).strip().lower()
    if not payload.get("agent"):
        payload["agent"] = r["agent"]
        if r["model"] and not payload.get("model"):
            payload["model"] = r["model"]
    if r["allow_opus"] and scrum_roles.is_opus(payload.get("model")):
        payload["_allow_opus"] = True
    if r["note"]:
        payload["_role_note"] = r["note"]


@app.post("/tasks", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, x_secret_key: str = Header(default=""),
                      x_client: str = Header(default="")):
    _check_auth(x_secret_key)
    payload = dict(body.payload or {})
    # Which client asked (iPad app, fleet-mcp on the MacBook, WhatsApp ...): the proof of
    # where work was initiated. A caller-supplied _origin wins; the header fills the gap.
    if x_client.strip() and not payload.get("_origin"):
        payload["_origin"] = x_client.strip()[:60]
    await _apply_role(payload)
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


@app.get("/llm-choices")
async def llm_choices(x_secret_key: str = Header(default="")) -> list[dict]:
    """The one list of LLM choices every client shows, each with the payload fragment it sends."""
    _check_auth(x_secret_key)
    return build_llm_choices(scrum_roles.load_policy().get("roles") or {})


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


# ── Scrum / usage summaries ──────────────────────────────────────────────────

_GROUPS = ("agent", "machine", "project", "sprint", "role", "type", "day")


@app.get("/scrum/usage")
async def scrum_usage(
    group_by: str = Query(default="agent"),
    since: str | None = Query(default=None, description="ISO date/time, inclusive (created_at)"),
    until: str | None = Query(default=None, description="ISO date/time, exclusive"),
    project: str | None = Query(default=None),
    sprint: str | None = Query(default=None),
    x_secret_key: str = Header(default=""),
):
    """Tasks, outcomes, agents/machines involved and token usage, grouped by agent, machine,
    project, sprint, task type or day. Usage covers fleet tasks only: it cannot see how much of a
    subscription quota is left, and tasks before 2026-10-30 have an agent but no token counts."""
    _check_auth(x_secret_key)
    if group_by not in _GROUPS:
        raise HTTPException(status_code=422, detail=f"group_by must be one of {list(_GROUPS)}")
    rows = _canon_machines(await db.usage_rows(since=since, until=until, project=project, sprint=sprint))
    return {
        "group_by": group_by, "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "filters": {"since": since, "until": until, "project": project, "sprint": sprint},
        "tasks": len(rows),
        "totals": (tracking.summarise(rows, "all") or [{}])[0],
        "groups": tracking.summarise(rows, group_by),
    }


# ── Scrum board: backlog + sprints + what the fleet did for them ─────────────

SCRUM_REPO = os.getenv("SCRUM_REPO", "~/Projects/github/techstartups/vizneoprojects")
_SCRUM_CACHE: dict = {"ts": 0.0, "data": None}
_SCRUM_TTL = 30


# A backlog that lists OPEN work only says nothing about completion from closed/total. None are left
# (distributed-infra, vantage and motethansen now carry dated closed items); add a project here if one
# is ever created that way. Reported, but flagged.
_OPEN_ONLY_BACKLOGS: set[str] = set()

# The board must not go stale because nobody pulled the notes repo: fast-forward it at most every
# _PULL_EVERY seconds (SCRUM_REPO_PULL=0 turns it off). A failed pull is shown, never fatal.
_PULL: dict = {"ts": 0.0, "ok": None, "error": None, "at": None}
_PULL_EVERY = 120


def _canon_machines(rows: list[dict]) -> list[dict]:
    """Old tasks name thinkpad three ways; fold aliases into the roster's canonical name."""
    try:
        machines = _load_machines()
    except (OSError, yaml.YAMLError):
        return rows
    alias = {a: name for name, cfg in machines.items() for a in (cfg.get("aliases") or [])}
    return [({**r, "assigned_to": alias.get(r.get("assigned_to"), r.get("assigned_to"))} if r.get("assigned_to") in alias else r)
            for r in rows]


def _repo_head(repo: str) -> dict:
    """Commit + date of the checkout we are reading, so a stale checkout is visible, not hidden."""
    try:
        r = subprocess.run(["git", "-C", os.path.expanduser(repo), "log", "-1", "--format=%h|%cI|%s"],
                           capture_output=True, text=True, timeout=5)
        h, d, subj = (r.stdout.strip().split("|", 2) + ["", ""])[:3]
        return {"commit": h, "committed_at": d, "subject": subj[:100]}
    except (subprocess.SubprocessError, OSError):
        return {}


async def _pull_scrum_repo() -> None:
    if os.getenv("SCRUM_REPO_PULL", "1") == "0" or time.time() - _PULL["ts"] < _PULL_EVERY:
        return
    _PULL["ts"] = time.time()
    before = _repo_head(SCRUM_REPO).get("commit")
    try:
        r = await asyncio.to_thread(subprocess.run, ["git", "-C", os.path.expanduser(SCRUM_REPO), "pull", "--ff-only", "-q"],
                                    capture_output=True, text=True, timeout=30)
        ok, err = r.returncode == 0, None if r.returncode == 0 else ((r.stderr or "").strip().splitlines() or ["pull failed"])[-1][:160]
    except (subprocess.SubprocessError, OSError) as exc:
        ok, err = False, f"{type(exc).__name__}"
    _PULL.update(ok=ok, error=err, at=datetime.utcnow().isoformat(timespec="seconds") + "Z")
    if _repo_head(SCRUM_REPO).get("commit") != before:
        _SCRUM_CACHE["ts"] = 0.0


@app.post("/scrum/refresh")
async def scrum_refresh(x_secret_key: str = Header(default="")):
    """Pull the notes repo and drop the cached board NOW, so a change just pushed shows immediately. Returns the commit the
    board is reading, so the caller can check it matches what it pushed."""
    _check_auth(x_secret_key)
    _PULL["ts"] = 0.0
    await _pull_scrum_repo()
    _SCRUM_CACHE["ts"] = 0.0
    return {"head": _repo_head(SCRUM_REPO).get("commit"), "pull": {k: _PULL[k] for k in ("at", "ok", "error")}}


@app.get("/scrum/sprints")
async def scrum_sprints(x_secret_key: str = Header(default="")):
    """Per project: backlog completion; per sprint: stories, points, burndown; joined with the
    fleet's recorded work (tasks tagged with a `story`): which agents and machines did it, tokens.
    Reads the vizneoprojects checkout on THIS machine (SCRUM_REPO); `repo` shows how fresh it is."""
    _check_auth(x_secret_key)
    await _pull_scrum_repo()
    now = time.time()
    if now - _SCRUM_CACHE["ts"] > _SCRUM_TTL or _SCRUM_CACHE["data"] is None:
        try:
            _SCRUM_CACHE["data"] = await asyncio.to_thread(scrum.load, SCRUM_REPO)
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"scrum repo not readable at {SCRUM_REPO}: {exc}")
        _SCRUM_CACHE["ts"] = now
    data = _SCRUM_CACHE["data"]
    rows = _canon_machines(await db.usage_rows())
    by_story = {g["key"]: g for g in tracking.summarise([r for r in rows if r.get("story")], "story")}
    projects = []
    for p in data["projects"]:
        sprints = []
        for sp in p["sprints"]:
            stories = []
            for st in sp["stories"]:
                f = by_story.get(st["id"] or "")
                stories.append({**st, "fleet": ({k: f[k] for k in ("tasks", "done", "agents", "machines",
                                                                       "tokens_total", "cost_usd")} if f else None)})
            fleet_agents = sorted({a for st in stories if st["fleet"] for a in st["fleet"]["agents"]})
            fleet_machines = sorted({m for st in stories if st["fleet"] for m in st["fleet"]["machines"]})
            sprints.append({**sp, "stories": stories, "agents_involved": fleet_agents,
                            "machines_involved": fleet_machines,
                            "agent_count": len(fleet_agents)})
        view = scrum_digest.backlog_view(p)
        p = {k: v for k, v in p.items() if k != "backlog_items"}          # the view carries what the page needs
        projects.append({**p, "backlog_view": view, "sprints": sprints, "backlog_open_work_only": p["project"] in _OPEN_ONLY_BACKLOGS,
                         "complete_pct": None if p["project"] in _OPEN_ONLY_BACKLOGS else p["complete_pct"]})
    recent = await db.list_tasks(limit=25)
    opus = None
    try:
        cap = (scrum_roles.load_policy().get("roles") or {}).get("product_owner", {}).get("monthly_cost_cap_usd")
        if cap:
            opus = {"spent": round(await _month_opus_cost(), 2), "cap": float(cap)}
    except Exception:                                    # the digest is advice; it must never break the board
        opus = None
    return {
        "digest": scrum_digest.build(projects, opus=opus),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "repo": {"path": data["repo"], **_repo_head(SCRUM_REPO),
                 "pull": {k: _PULL[k] for k in ("at", "ok", "error")}},
        "projects": projects,
        "fleet": {"totals": (tracking.summarise(rows, "all") or [{}])[0],
                  "by_agent": tracking.summarise(rows, "agent"), "by_machine": tracking.summarise(rows, "machine"),
                  "by_role": tracking.summarise([r for r in rows if r.get("role")], "role")},
        "recent_tasks": [{"id": t.id[:8], "type": t.type.value if hasattr(t.type, "value") else str(t.type),
                          "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                          "machine": t.assigned_to, "agent": t.agent, "model": t.model,
                          "tokens": t.tokens_total if t.tokens_total is not None else
                          ((t.tokens_in or 0) + (t.tokens_out or 0) or None),
                          "origin": (t.payload or {}).get("_origin") or None,        # None = not recorded (created_by is just the orchestrator)
                          "via": t.created_by,
                          "project": t.project, "sprint": t.sprint, "story": t.story, "role": t.role,
                          "started_at": t.started_at, "finished_at": t.finished_at,
                          "created_at": t.created_at.isoformat(timespec="seconds")} for t in recent],
        "notes": ["Story status comes from the product backlog when it knows the ID (it carries the evidence), "
                  "else from the sprint file. `points_remaining_as_written` is the sprint file uncorrected.",
                  "Burndown history is only what a sprint file recorded; the last point is today's computed value.",
                  "Fleet usage covers tasks tagged with a story; tokens exist only for tasks run since 2026-10-30.",
                  "Claude tokens_in includes the cached context re-read on every call (tens of thousands per call), "
                  "so compare agents by tokens_out and task count as well as the input total.",
                  "backlog_open_work_only: that project's backlog lists open work only, so its completion % is not shown."],
    }


# ── Machine roster ───────────────────────────────────────────────────────────

def _load_machines() -> dict:
    with open(_MACHINES_CONFIG) as f:
        return yaml.safe_load(f).get("machines", {})


def _lan_ips_for(name: str, cfg: dict) -> dict[str, str]:
    ips = _lan_ips.get(name)
    for alias in cfg.get("aliases", []) or []:
        ips = ips or _lan_ips.get(alias)
    return ips or {}


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
            "tailscale_path": (tsinfo or {}).get("path"),      # direct | relay:<region> | None (self)
            "tailscale_addr": (tsinfo or {}).get("cur_addr"),  # where Tailscale is reaching it right now
            "lan_ips": _lan_ips_for(name, cfg),                # {iface: ip} the worker reported
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
