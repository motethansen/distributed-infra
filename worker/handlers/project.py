"""Autonomous project lifecycle (#18) — intake → plan → approve → execute.

The capstone: a project registry + lifecycle verbs that wrap the #8 Plan-and-Execute
engine with an approval gate. Pinned to macbook-pro so config/projects.yaml is a
single source of truth; it enqueues planning + execution as sub-tasks on the
project's chosen machine.

payload: {subcommand, args}
  start  <name> [on <machine>]: <goal>   — create + draft a plan (status=planning)
  review <name>: <path>                  — read an existing repo, propose a plan
  plan   <name>                          — show the current plan
  go     <name>                          — APPROVAL gate → scaffold + run the #8 engine
  status [<name>] / list                 — registry view
  stop   <name>                          — kill switch (status=stopped)

Autonomy: recorded per project (default L2 = develop-but-gate). Money/publish gates
land with #11/#16; for now the `go` approval is the gate and execution is the #8 engine.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from shared.models import Task

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", "")
_REGISTRY = Path(__file__).parent.parent.parent / "config" / "projects.yaml"

_MACHINE_ALIASES = {
    "thinkpad": "thinkpad-x1", "thinkpad-x1": "thinkpad-x1", "linux": "thinkpad-x1",
    "mini": "mac-mini", "macmini": "mac-mini", "mac-mini": "mac-mini",
    "macbook": "macbook-pro", "mbp": "macbook-pro", "macbook-pro": "macbook-pro",
}
_DEFAULT_MACHINE = "mac-mini"


def _headers() -> dict:
    return {"x-secret-key": SECRET_KEY, "Content-Type": "application/json"}


def _load() -> dict:
    if _REGISTRY.exists():
        with open(_REGISTRY, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("projects", {}) or {}
    return {}


def _save(projects: dict) -> None:
    _REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = _REGISTRY.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump({"projects": projects}, f, allow_unicode=True, sort_keys=False)
    tmp.replace(_REGISTRY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _enqueue_and_wait(task_type: str, payload: dict, timeout: int) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{ORCHESTRATOR_URL}/tasks", headers=_headers(),
                             json={"type": task_type, "payload": payload, "notes": "project step"})
        if r.status_code != 201:
            return {"ok": False, "error": f"enqueue failed ({r.status_code})"}
        tid = r.json().get("id")
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"enqueue error: {e!r}"}

    waited = 0
    while waited < timeout:
        await asyncio.sleep(3)
        waited += 3
        # Guard every poll: a single transient network hiccup over a long wait must
        # not kill the whole orchestration — just retry on the next tick.
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                rr = await c.get(f"{ORCHESTRATOR_URL}/tasks/{tid}", headers=_headers())
        except httpx.HTTPError:
            continue
        if rr.status_code != 200:
            continue
        d = rr.json()
        st = d.get("status")
        if st in ("done", "failed", "needs_human"):
            result = d.get("result") or {}
            if st == "done":
                return {"ok": True, "output": result.get("response", "")}
            return {"ok": False, "error": result.get("error") or d.get("notes") or st, "status": st}
    return {"ok": False, "error": f"{task_type} timed out"}


def _resolve_machine(name: str) -> str:
    return _MACHINE_ALIASES.get((name or "").strip().lower(), _DEFAULT_MACHINE)


async def handle_project(task: Task) -> dict:
    p = task.payload or {}
    sub = (p.get("subcommand") or "").strip().lower()
    args = (p.get("args") or "").strip()
    projects = _load()

    if sub in ("list", ""):
        return {"response": _fmt_list(projects)}

    if sub == "status":
        name = args.split()[0] if args else ""
        if name and name in projects:
            return {"response": _fmt_one(name, projects[name])}
        return {"response": _fmt_list(projects)}

    if sub == "stop":
        name = args.split()[0] if args else ""
        if name not in projects:
            return {"response": f"No project '{name}'."}
        projects[name]["status"] = "stopped"
        projects[name]["updated_at"] = _now()
        _save(projects)
        return {"response": f"🛑 Project '{name}' stopped."}

    if sub == "plan":
        name = args.split()[0] if args else ""
        if name not in projects:
            return {"response": f"No project '{name}'. Use `project start {name}: <goal>`."}
        return {"response": _fmt_one(name, projects[name])}

    if sub in ("start", "review"):
        return await _intake(sub, args, projects)

    if sub == "go":
        return await _go(args, projects)

    return {"response": "Usage: project <start|review|plan|go|status|list|stop> …\n"
                        "e.g. project start todoapp on thinkpad: build a CLI todo app in python"}


async def _intake(sub: str, args: str, projects: dict) -> dict:
    # parse: <name> [on <machine>] : <goal>   (':' optional for start)
    m = re.match(r"(?P<name>\S+)(?:\s+on\s+(?P<machine>\S+))?\s*(?::\s*(?P<goal>.+))?$",
                 args, re.IGNORECASE | re.DOTALL)
    if not m or not m.group("name"):
        return {"response": f"Usage: project {sub} <name> [on <machine>]: <goal-or-path>"}
    name = m.group("name")
    machine = _resolve_machine(m.group("machine") or _DEFAULT_MACHINE)
    rest = (m.group("goal") or "").strip()

    if sub == "review":
        path = rest or (projects.get(name, {}).get("path"))
        if not path:
            return {"response": f"Usage: project review <name>: <repo-path>"}
        goal = f"Review the existing project and propose next steps."
        prompt = (f"Review the repository at {path} on {machine}. Summarize what it is and its "
                  f"state, then propose a concise step-by-step plan (3-7 steps) for sensible next "
                  f"work. End with any clarifying questions for the owner.")
        plan_res = await _enqueue_and_wait(
            "agent_run", {"agent": "claude", "prompt": prompt, "cwd": path,
                          "_target_machine": machine, "timeout": 300}, timeout=330)
    else:  # start
        if not rest:
            return {"response": f"Usage: project start {name} [on <machine>]: <goal>"}
        goal = rest
        path = f"~/Projects/{name}"
        prompt = (f"Draft a concise step-by-step plan (3-7 steps) to achieve this project goal, "
                  f"to be built at {path} on {machine}. List the steps, then list any clarifying "
                  f"questions for the owner before building.\n\nGOAL: {goal}")
        plan_res = await _enqueue_and_wait(
            "agent_run", {"agent": "claude", "prompt": prompt, "task_kind": "planning",
                          "_target_machine": machine, "timeout": 300}, timeout=330)

    plan_text = plan_res.get("output") if plan_res.get("ok") else f"(planning failed: {plan_res.get('error')})"
    projects[name] = {
        "machine": machine, "path": path, "goal": goal, "autonomy": "L2",
        "status": "planning", "plan": plan_text, "created_at": _now(), "updated_at": _now(),
    }
    _save(projects)
    return {"response": (f"📋 Project '{name}' ({machine}:{path}) — status: planning\n\n"
                         f"{plan_text}\n\n"
                         f"Reply to refine, or send `project go {name}` to scaffold + build it.")}


async def _go(args: str, projects: dict) -> dict:
    name = args.split()[0] if args else ""
    if name not in projects:
        return {"response": f"No project '{name}'. Use `project start {name}: <goal>` first."}
    proj = projects[name]
    if proj.get("status") == "stopped":
        return {"response": f"Project '{name}' is stopped. Start a new one or change its status."}

    proj["status"] = "executing"
    proj["updated_at"] = _now()
    _save(projects)

    machine, path, goal, plan = proj["machine"], proj["path"], proj["goal"], proj.get("plan", "")
    exec_goal = (f"{goal}\n\nApproved plan:\n{plan}\n\nSet up the project at {path} (run "
                 f"`mkdir -p {path}` and `git init` if it's new), then implement it. "
                 f"Put all files under {path}.")
    res = await _enqueue_and_wait(
        "plan", {"goal": exec_goal, "target_machine": machine, "cwd": path, "max_steps": 6},
        timeout=1500)

    projects = _load()  # reload in case it changed
    if name in projects:
        projects[name]["status"] = "done" if res.get("ok") else "needs_human"
        projects[name]["updated_at"] = _now()
        _save(projects)

    if res.get("ok"):
        return {"response": f"✅ Project '{name}' executed.\n\n{res.get('output', '')[:1200]}"}
    return {"needs_human": True,
            "notes": f"Project '{name}' did not converge: {res.get('error')}",
            "action": f"Review {machine}:{path} and advise; `project go {name}` to retry."}


def _fmt_list(projects: dict) -> str:
    if not projects:
        return "📁 No projects yet. Start one: `project start <name> [on <machine>]: <goal>`"
    icon = {"planning": "📝", "executing": "⏳", "done": "✓", "needs_human": "👀", "stopped": "🛑"}
    lines = ["📁 Projects:"]
    for n, pr in projects.items():
        lines.append(f"{icon.get(pr.get('status'), '·')} {n} [{pr.get('machine')}] — {pr.get('status')}: {pr.get('goal','')[:50]}")
    return "\n".join(lines)


def _fmt_one(name: str, pr: dict) -> str:
    return (f"📁 {name} [{pr.get('machine')}:{pr.get('path')}]\n"
            f"status: {pr.get('status')} · autonomy: {pr.get('autonomy')}\n"
            f"goal: {pr.get('goal')}\n\nplan:\n{pr.get('plan','(none)')[:1000]}")
