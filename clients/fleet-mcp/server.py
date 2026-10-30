#!/usr/bin/env python3
"""Fleet MCP server — gives any MCP-capable harness access to the distributed agents.

Runs locally over stdio (Claude Code, Codex, OpenCode, Antigravity, pi …) and talks
to the orchestrator queue over Tailscale. The agents themselves run on the workers
under their own logins (claude / codex / agy subscriptions, DeepSeek API key), and
the worker fails over between them when one hits its usage limit
(agents/failover.py). Nothing here holds an LLM credential.

Tools
  fleet_ask        send a prompt to the fleet; optional named conversation (multi-turn)
  fleet_result     fetch / wait for a task's result
  fleet_tasks      recent tasks in the queue
  fleet_machines   workers, liveness and capabilities
  fleet_end_conversation   forget a named conversation

Config (env, else read from the repo's .env — the secret never goes in a harness
config file): SECRET_KEY, ORCHESTRATOR_URLS (comma list, tried in order),
FLEET_MCP_STATE (conversation store, default <repo>/data/fleet_mcp_state.json),
VANTAGE_URL / VANTAGE_INGEST_KEY (health reports, component `fleet-mcp`).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parent.parent
DEFAULT_URLS = "http://100.76.214.54:8000,http://100.112.241.6:8000"
MAX_WAIT = 110          # stay under harness tool-call timeouts (Codex default is 60–120 s)
HISTORY_TURNS = 20
TURN_CHARS = 4000


# ── config ────────────────────────────────────────────────────────────────────
def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_config(env: dict[str, str] | None = None, dotenv_path: Path | None = None) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    file_env = _dotenv(dotenv_path or REPO_ROOT / ".env")
    get = lambda k, d="": env.get(k) or file_env.get(k) or d  # noqa: E731
    urls = [u.strip().rstrip("/") for u in get("ORCHESTRATOR_URLS", DEFAULT_URLS).split(",") if u.strip()]
    return {
        "secret": get("SECRET_KEY"),
        "urls": urls,
        "state_file": Path(get("FLEET_MCP_STATE", str(REPO_ROOT / "data" / "fleet_mcp_state.json"))),
        "vantage_url": get("VANTAGE_URL", "http://100.76.214.54:8055"),
        "vantage_key": get("VANTAGE_INGEST_KEY") or None,
    }


# ── orchestrator client ───────────────────────────────────────────────────────
class FleetError(RuntimeError):
    pass


class Fleet:
    def __init__(self, cfg: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None):
        if not cfg.get("secret"):
            raise FleetError("SECRET_KEY not set (env or distributed-infra/.env)")
        self.cfg = cfg
        self._transport = transport

    async def _call(self, method: str, path: str, **kw) -> Any:
        errors = []
        for base in self.cfg["urls"]:
            try:
                async with httpx.AsyncClient(timeout=15, transport=self._transport) as c:
                    r = await c.request(method, f"{base}{path}",
                                        headers={"x-secret-key": self.cfg["secret"], "x-client": f"fleet-mcp@{socket.gethostname().split('.')[0]}"}, **kw)
                if r.status_code >= 500:
                    errors.append(f"{base}: HTTP {r.status_code}")
                    continue  # try the other orchestrator (active-active)
                if r.status_code >= 400:
                    raise FleetError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
                return json.loads(r.text, strict=False)
            except httpx.HTTPError as e:
                errors.append(f"{base}: {type(e).__name__}")
        raise FleetError("no orchestrator reachable — " + "; ".join(errors))

    async def create(self, ttype: str, payload: dict, notes: str) -> dict:
        return await self._call("POST", "/tasks", json={"type": ttype, "payload": payload, "notes": notes[:80]})

    async def get(self, task_id: str) -> dict:
        return await self._call("GET", f"/tasks/{task_id}")

    async def list(self, status: str | None, limit: int) -> list:
        params = {"limit": max(1, min(limit, 100))}
        if status:
            params["status"] = status
        return await self._call("GET", "/tasks", params=params)

    async def machines(self) -> list:
        return await self._call("GET", "/machines")


# ── conversation store (local) ────────────────────────────────────────────────
class State:
    """Named conversations + which task belongs to which, so a reply can be
    recorded whichever tool call sees it finish. Re-read before every write:
    several harnesses may run this server at once."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("conversations", {})
                data.setdefault("tasks", {})
                return data
        except (OSError, ValueError):
            pass
        return {"conversations": {}, "tasks": {}}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def _trim(history: list) -> list:
    return history[-HISTORY_TURNS:]


# ── result formatting ─────────────────────────────────────────────────────────
TERMINAL = {"done", "failed", "needs_human"}


def summarise(task: dict) -> dict:
    """Compact view of a task for the harness."""
    result = task.get("result") or {}
    if not isinstance(result, dict):
        result = {"response": str(result)}
    out: dict[str, Any] = {"task_id": task.get("id"), "status": task.get("status")}
    if task.get("status") == "done":
        out["answered_by"] = result.get("answered_by") or result.get("agent")
        out["response"] = result.get("response") or result.get("stdout") or result
    elif task.get("status") in ("failed", "needs_human"):
        out["error"] = result.get("error") or task.get("notes")
    else:
        out["note"] = "still running — call fleet_result with this task_id (and wait_seconds) to get the answer"
    if result.get("failover"):
        out["failover"] = [
            {k: f.get(k) for k in ("agent", "reason", "until") if f.get(k)} for f in result["failover"]
        ]
    return out


async def wait_for(fleet: Fleet, task_id: str, wait_seconds: float, poll: float = 3.0) -> dict:
    deadline = time.monotonic() + max(0.0, min(wait_seconds, MAX_WAIT))
    while True:
        task = await fleet.get(task_id)
        if task.get("status") in TERMINAL or time.monotonic() >= deadline:
            return task
        await asyncio.sleep(poll)


# ── tool logic (kept free of MCP glue so it is testable) ──────────────────────
class Tools:
    def __init__(self, fleet: Fleet, state: State, reporter=None):
        self.fleet, self.state, self.reporter = fleet, state, reporter
        self._last_report = 0.0

    def _health(self, ok: bool, error: str | None = None) -> None:
        """Push to Vantage: errors immediately, healthy at most once a minute."""
        if not self.reporter:
            return
        now = time.time()
        if ok and now - self._last_report < 60:
            return
        self._last_report = now
        self.reporter.push(status="up" if ok else "degraded", errors=[error] if error else [],
                           checks={"orchestrator": "up" if ok else "down", "host": socket.gethostname()})

    async def _guard(self, coro):
        try:
            out = await coro
            self._health(True)
            return out
        except FleetError as e:
            self._health(False, str(e))
            return {"error": str(e)}

    def _record(self, task: dict) -> None:
        """If this finished task belongs to a conversation, append the reply."""
        if task.get("status") != "done":
            return
        data = self.state.load()
        conv_name = data["tasks"].pop(task.get("id"), None)
        conv = data["conversations"].get(conv_name) if conv_name else None
        if conv is None:
            if conv_name:
                self.state.save(data)
            return
        result = task.get("result") or {}
        answered = result.get("answered_by") or result.get("agent") or conv.get("agent")
        conv["history"] = _trim(conv.get("history", []) + [
            {"role": "assistant", "text": str(result.get("response") or "")[:TURN_CHARS], "agent": answered}])
        if answered == conv.get("agent") and result.get("session_id"):
            conv["session_id"] = result["session_id"]
        conv["updated"] = time.time()
        self.state.save(data)

    async def ask(self, prompt: str, agent: str | None = None, task_kind: str | None = None,
                  sensitivity: str | None = None, machine: str | None = None, cwd: str | None = None,
                  conversation: str | None = None, wait_seconds: float = 90, timeout: int = 1800,
                  failover: bool = True) -> dict:
        if not prompt.strip():
            return {"error": "prompt is empty"}
        if not agent and not task_kind and not sensitivity:
            task_kind = "default"
        payload: dict[str, Any] = {"prompt": prompt, "timeout": int(timeout), "failover": bool(failover)}
        for k, v in (("agent", agent), ("task_kind", task_kind), ("sensitivity", sensitivity), ("cwd", cwd)):
            if v:
                payload[k] = v
        if machine:
            payload["_target_machine"] = machine

        data = None
        if conversation:
            data = self.state.load()
            conv = data["conversations"].get(conversation)
            if conv is None:
                conv = {"agent": agent or "claude", "session_id": str(uuid.uuid4()),
                        "history": [], "created": time.time()}
                data["conversations"][conversation] = conv
                payload["resume"] = False
            else:
                payload["resume"] = True
                payload["history"] = list(conv.get("history", []))
            payload.setdefault("agent", conv["agent"])
            payload["session_id"] = conv["session_id"]
            conv["history"] = _trim(conv.get("history", []) + [{"role": "user", "text": prompt[:TURN_CHARS]}])
            conv["updated"] = time.time()

        async def go():
            task = await self.fleet.create("agent_run", payload, notes=f"mcp: {prompt[:70]}")
            if data is not None:
                # Re-read: another harness may have written meanwhile.
                fresh = self.state.load()
                fresh["conversations"][conversation] = data["conversations"][conversation]
                fresh["tasks"][task["id"]] = conversation
                self.state.save(fresh)
            task = await wait_for(self.fleet, task["id"], wait_seconds)
            self._record(task)
            return summarise(task)

        return await self._guard(go())

    async def result(self, task_id: str, wait_seconds: float = 60) -> dict:
        async def go():
            task = await wait_for(self.fleet, task_id, wait_seconds)
            self._record(task)
            return summarise(task)
        return await self._guard(go())

    async def tasks(self, status: str | None = None, limit: int = 15) -> dict:
        async def go():
            rows = await self.fleet.list(status, limit)
            return {"tasks": [{"task_id": t.get("id"), "type": t.get("type"), "status": t.get("status"),
                               "created_at": t.get("created_at"), "notes": (t.get("notes") or "")[:100]}
                              for t in rows]}
        return await self._guard(go())

    async def machines(self) -> dict:
        async def go():
            rows = await self.fleet.machines()
            return {"machines": [{"name": m.get("name"), "worker_active": m.get("worker_active"),
                                  "last_seen_secs": m.get("last_seen_ago_secs"),
                                  "can_run_agents": "agent_run" in (m.get("capabilities") or [])}
                                 for m in rows]}
        return await self._guard(go())

    def end_conversation(self, conversation: str) -> dict:
        data = self.state.load()
        had = data["conversations"].pop(conversation, None)
        data["tasks"] = {k: v for k, v in data["tasks"].items() if v != conversation}
        self.state.save(data)
        return {"ended": bool(had), "conversation": conversation}


# ── MCP glue ──────────────────────────────────────────────────────────────────
def build_server():
    from mcp.server.fastmcp import FastMCP

    from vantage_reporter import Reporter

    cfg = load_config()
    reporter = Reporter("fleet-mcp", url=cfg["vantage_url"], key=cfg["vantage_key"])
    tools = Tools(Fleet(cfg), State(cfg["state_file"]), reporter)
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp = FastMCP("fleet", log_level="WARNING", instructions=(
        "Access to Michael's distributed agent fleet (mac-mini + thinkpad workers). "
        "fleet_ask runs a prompt on a fleet agent (Claude Code, Codex, Antigravity or DeepSeek); "
        "if that agent is out of usage the fleet switches to the next one automatically and the "
        "result says who answered. Use `conversation` to keep a multi-turn chat going across calls "
        "— and across harnesses. Long jobs return a task_id: poll with fleet_result."))

    @mcp.tool()
    async def fleet_ask(prompt: str, agent: str | None = None, task_kind: str | None = None,
                        sensitivity: str | None = None, machine: str | None = None,
                        cwd: str | None = None, conversation: str | None = None,
                        wait_seconds: float = 90, timeout: int = 1800, failover: bool = True) -> dict:
        """Run a prompt on a fleet agent and return its answer.

        agent: claude | codex | agy | deepseek | content | social (omit to route by task_kind).
        task_kind: e.g. code, plan, reasoning, summarize, classify — picks the cheapest fit.
        sensitivity: "private" keeps the work off non-private providers (never DeepSeek).
        machine: pin a worker (mac-mini, thinkpad-x1); default lets the queue choose.
        cwd: working directory ON THE WORKER (e.g. ~/Projects/<repo>), not this machine.
        conversation: a name to continue a multi-turn chat (history kept on this machine).
        wait_seconds: how long to wait (max 110); if still running you get a task_id.
        failover: false pins the task to its first agent.
        """
        return await tools.ask(prompt, agent, task_kind, sensitivity, machine, cwd, conversation,
                               wait_seconds, timeout, failover)

    @mcp.tool()
    async def fleet_result(task_id: str, wait_seconds: float = 60) -> dict:
        """Get a fleet task's result, waiting up to wait_seconds (max 110) for it to finish."""
        return await tools.result(task_id, wait_seconds)

    @mcp.tool()
    async def fleet_tasks(status: str | None = None, limit: int = 15) -> dict:
        """Recent fleet tasks. status: pending | in_progress | done | failed | needs_human."""
        return await tools.tasks(status, limit)

    @mcp.tool()
    async def fleet_machines() -> dict:
        """Fleet workers: whether each is active, last seen, and whether it can run agents."""
        return await tools.machines()

    @mcp.tool()
    def fleet_end_conversation(conversation: str) -> dict:
        """Forget a named conversation so the next fleet_ask with that name starts fresh."""
        return tools.end_conversation(conversation)

    return mcp


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(_HERE))
    build_server().run()  # stdio
