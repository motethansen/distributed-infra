"""
WhatsApp ↔ distributed-infra bridge
────────────────────────────────────
Runs on Mac Mini alongside Waha (Docker).
Receives WhatsApp messages via Waha webhook → posts tasks to the
orchestrator queue on MacBook → replies with results via Waha REST API.

Usage:
  Send commands to yourself on WhatsApp from your phone.
  Waha delivers them here; bridge replies when the task finishes.
"""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Response

# ── Config ────────────────────────────────────────────────────────────────────
WAHA_URL         = os.getenv("WAHA_URL", "http://localhost:3000")
WAHA_API_KEY     = os.getenv("WAHA_API_KEY", "")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://100.97.176.37:8000")
SECRET_KEY       = os.getenv("INFRA_SECRET_KEY", "")
WAHA_SESSION     = "default"
POLL_INTERVAL    = 8    # seconds between completion checks
TASK_TIMEOUT     = 360  # seconds before giving up

# In-memory map: task_id → {chat_id, started_at}
_pending: dict[str, dict] = {}

# Prefixes the bridge uses in its own replies — never treat these as commands.
# Waha echoes the bridge's outbound messages back via webhook (fromMe=true),
# so without this filter every reply would loop back as an "unknown" command.
_REPLY_PREFIXES = (
    "✓", "✗", "⏳", "✅", "❌", "🟢", "🔴", "⚪", "👀", "📋", "⏱", "⚙",
    "Commands:", "Machines:", "Needs review:", "Failed:",
)


# ── Queue client ──────────────────────────────────────────────────────────────
def _headers() -> dict:
    return {"x-secret-key": SECRET_KEY, "Content-Type": "application/json"}


def _waha_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        h["X-Api-Key"] = WAHA_API_KEY
    return h


async def _send_wa(chat_id: str, text: str) -> None:
    async with httpx.AsyncClient() as c:
        await c.post(f"{WAHA_URL}/api/sendText",
            headers=_waha_headers(),
            json={"session": WAHA_SESSION, "chatId": chat_id, "text": text},
            timeout=10)


async def _create_task(task_type: str, payload: dict, notes: str = "") -> str | None:
    body = {"type": task_type, "payload": payload}
    if notes:
        body["notes"] = notes
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{ORCHESTRATOR_URL}/tasks",
            headers=_headers(), json=body, timeout=10)
        if r.status_code == 201:
            return r.json().get("id")
    return None


async def _get_task(task_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}",
            headers=_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
    return None


async def _list_tasks(status: str | None = None) -> list[dict]:
    params = {"limit": 15}
    if status:
        params["status"] = status
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{ORCHESTRATOR_URL}/tasks",
            headers=_headers(), params=params, timeout=10)
        return r.json() if r.status_code == 200 else []


async def _list_machines() -> list[dict]:
    async with httpx.AsyncClient() as c:
        try:
            r = await c.get(f"{ORCHESTRATOR_URL}/machines",
                headers=_headers(), timeout=10)
        except httpx.HTTPError:
            return []
    return r.json() if r.status_code == 200 else []


# ── Formatters ────────────────────────────────────────────────────────────────
def _fmt_queue(tasks: list[dict]) -> str:
    if not tasks:
        return "📋 Queue is empty."
    icons = {"done": "✓", "failed": "✗", "in_progress": "⏳",
             "pending": "·", "needs_human": "👀", "claimed": "⚙"}
    lines = []
    for t in tasks:
        icon    = icons.get(t.get("status", ""), "?")
        tid     = (t.get("id") or "")[:8]
        machine = t.get("assigned_to") or (t.get("payload") or {}).get("_target_machine") or "any"
        agent   = (t.get("payload") or {}).get("agent", "-")
        notes   = (t.get("notes") or "")[:45]
        lines.append(f"{icon} {tid} [{machine}/{agent}] {notes}")
    return "📋 Queue:\n" + "\n".join(lines)


def _fmt_age(secs: int | None) -> str:
    if secs is None:
        return "never seen"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _fmt_status(machines: list[dict], tasks: list[dict]) -> str:
    if not machines:
        return "🔴 Could not reach orchestrator."

    # Resolve worker aliases (historical names in task DB) back to canonical name
    alias_map: dict[str, str] = {}
    for m in machines:
        alias_map[m["name"]] = m["name"]
        for a in m.get("aliases", []):
            alias_map[a] = m["name"]

    stats: dict[str, dict] = {}
    for t in tasks:
        raw = t.get("assigned_to") or "unassigned"
        name = alias_map.get(raw, raw)
        if name not in stats:
            stats[name] = {"active": 0, "done": 0, "failed": 0}
        s = t.get("status", "")
        if s in ("claimed", "in_progress"):
            stats[name]["active"] += 1
        elif s == "done":
            stats[name]["done"] += 1
        elif s == "failed":
            stats[name]["failed"] += 1

    lines = []
    for m in machines:
        name   = m["name"]
        role   = m.get("role", "worker")
        online = m.get("online", False)
        st     = stats.get(name, {"active": 0, "done": 0, "failed": 0})
        icon   = "🟢" if online else "🔴"
        suffix = "" if online else f"  (last seen {_fmt_age(m.get('last_seen_ago_secs'))})"
        lines.append(
            f"{icon} {name} [{role}]  active:{st['active']}  done:{st['done']}  failed:{st['failed']}{suffix}"
        )
    return "Machines:\n" + "\n".join(lines)


# ── Command parser ────────────────────────────────────────────────────────────
def _parse(text: str) -> tuple[str, dict]:
    t = text.strip()
    tl = t.lower()

    if tl in ("status", "/status", "s"):
        return "status", {}
    if tl in ("queue", "/queue", "q"):
        return "queue", {}
    if tl in ("review", "/review"):
        return "review", {}
    if tl in ("failures", "/failures", "fail"):
        return "failures", {}
    if tl in ("help", "/help", "?", "h"):
        return "help", {}

    # assign <description> [--machine=X] [--agent=Y] [--type=Z]
    if re.match(r"^/?assign\s+", t, re.IGNORECASE):
        body    = re.sub(r"^/?assign\s+", "", t, flags=re.IGNORECASE)
        machine = re.search(r"--machine=(\S+)", body)
        agent   = re.search(r"--agent=(\S+)",   body)
        ttype   = re.search(r"--type=(\S+)",    body)
        desc    = re.sub(r"--\w+=\S+", "", body).strip()
        return "assign", {
            "description": desc,
            "machine":     machine.group(1) if machine else "mac-mini",
            "agent":       agent.group(1)   if agent   else "claude",
            "type":        ttype.group(1)   if ttype   else "agent_run",
        }

    # run <agent> <prompt>  →  agent_run on mac-mini
    if re.match(r"^/?run\s+", t, re.IGNORECASE):
        body  = re.sub(r"^/?run\s+", "", t, flags=re.IGNORECASE)
        parts = body.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("claude", "gemini", "codex", "groq"):
            return "run", {"agent": parts[0].lower(), "prompt": parts[1]}
        return "run", {"agent": "claude", "prompt": body}

    return "unknown", {"text": t}


# ── Result poller ─────────────────────────────────────────────────────────────
async def _poll_loop() -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        now = datetime.now(timezone.utc).timestamp()

        for task_id, meta in list(_pending.items()):
            if now - meta["started_at"] > TASK_TIMEOUT:
                await _send_wa(meta["chat_id"],
                    f"⏱ Task `{task_id[:8]}` timed out after {TASK_TIMEOUT}s.\n"
                    f"Check: da › review")
                _pending.pop(task_id, None)
                continue

            task = await _get_task(task_id)
            if not task:
                continue

            status = task.get("status")
            result = task.get("result") or {}
            response_text = (result.get("response") or result.get("error") or "")[:1400]

            if status == "done":
                await _send_wa(meta["chat_id"],
                    f"✅ Done  [{task_id[:8]}]\n\n{response_text}")
                _pending.pop(task_id, None)

            elif status == "failed":
                await _send_wa(meta["chat_id"],
                    f"❌ Failed  [{task_id[:8]}]\n{response_text[:600]}")
                _pending.pop(task_id, None)

            elif status == "needs_human":
                notes = (task.get("notes") or "")[:400]
                await _send_wa(meta["chat_id"],
                    f"👀 Needs input  [{task_id[:8]}]\n{notes}\n\nRun: da › review")
                _pending.pop(task_id, None)


# ── Waha session config ──────────────────────────────────────────────────────
# The webhook URL the bridge expects Waha to call. Container talks to the host
# via host.docker.internal, so this URL is relative to Waha's container — not
# the bridge's process.
WAHA_WEBHOOK_URL    = f"http://host.docker.internal:{int(os.getenv('BRIDGE_PORT', '3001'))}/webhook"
WAHA_WEBHOOK_EVENTS = ["message.any"]


async def _ensure_waha_config() -> None:
    """Make sure the Waha session has the webhook + events the bridge expects.

    Without this, deleting waha-sessions/ (or any Waha session reset) would
    silently disable command replies until someone manually PUTs the config.
    Idempotent: if the config already matches, this is a no-op.
    """
    async with httpx.AsyncClient() as c:
        try:
            r = await c.get(f"{WAHA_URL}/api/sessions/{WAHA_SESSION}",
                headers=_waha_headers(), timeout=5)
        except httpx.HTTPError as e:
            print(f"[waha-config] could not reach Waha: {e}", flush=True)
            return
        if r.status_code != 200:
            print(f"[waha-config] GET session returned {r.status_code}; skipping", flush=True)
            return

        cfg = (r.json() or {}).get("config") or {}
        webhooks = cfg.get("webhooks") or []
        wanted = {"url": WAHA_WEBHOOK_URL, "events": WAHA_WEBHOOK_EVENTS}
        already = any(
            w.get("url") == wanted["url"] and set(w.get("events") or []) >= set(wanted["events"])
            for w in webhooks
        )
        if already:
            return

        body = {"config": {"webhooks": [wanted]}}
        try:
            pr = await c.put(f"{WAHA_URL}/api/sessions/{WAHA_SESSION}",
                headers=_waha_headers(), json=body, timeout=10)
            print(f"[waha-config] PUT webhook config → {pr.status_code}", flush=True)
        except httpx.HTTPError as e:
            print(f"[waha-config] PUT failed: {e}", flush=True)


# ── App lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    await _ensure_waha_config()
    asyncio.create_task(_poll_loop())
    yield

app = FastAPI(lifespan=_lifespan)


# ── Webhook ───────────────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    if data.get("event") not in ("message", "message.any"):
        return Response(status_code=200)

    msg     = data.get("payload", {})
    key     = (msg.get("_data") or {}).get("key") or {}
    # Prefer the real WhatsApp address (remoteJidAlt) when WhatsApp's LID addressing is used
    chat_id = key.get("remoteJidAlt") or msg.get("from", "")
    body    = (msg.get("body") or "").strip()
    is_me   = msg.get("fromMe", False)

    # Only process messages sent by the user from their own phone (self-chat)
    if not is_me:
        return Response(status_code=200)

    # Ignore the bridge's own replies (they always start with a known emoji/prefix)
    if any(body.startswith(p) for p in _REPLY_PREFIXES):
        return Response(status_code=200)

    if not body:
        return Response(status_code=200)

    cmd, kwargs = _parse(body)

    if cmd == "status":
        machines = await _list_machines()
        tasks    = await _list_tasks()
        await _send_wa(chat_id, _fmt_status(machines, tasks))

    elif cmd == "queue":
        tasks = await _list_tasks()
        await _send_wa(chat_id, _fmt_queue(tasks))

    elif cmd == "review":
        tasks = await _list_tasks(status="needs_human")
        if not tasks:
            await _send_wa(chat_id, "✅ Nothing needs your attention.")
        else:
            lines = [f"👀 {(t.get('id') or '')[:8]} — {(t.get('notes') or '')[:55]}"
                     for t in tasks]
            await _send_wa(chat_id, "Needs review:\n" + "\n".join(lines))

    elif cmd == "failures":
        tasks = await _list_tasks(status="failed")
        if not tasks:
            await _send_wa(chat_id, "✅ No failed tasks.")
        else:
            lines = [
                f"✗ {(t.get('id') or '')[:8]} — "
                f"{((t.get('result') or {}).get('error') or t.get('notes') or '')[:55]}"
                for t in tasks
            ]
            await _send_wa(chat_id, "Failed:\n" + "\n".join(lines))

    elif cmd == "assign":
        machine = kwargs["machine"]
        agent   = kwargs["agent"]
        ttype   = kwargs["type"]
        desc    = kwargs["description"]
        if not desc:
            await _send_wa(chat_id, "❌ No task description. Usage: assign <task> [--machine=X] [--agent=Y]")
            return Response(status_code=200)

        payload = {"agent": agent, "prompt": desc, "_target_machine": machine}
        task_id = await _create_task(ttype, payload, notes=desc[:80])
        if task_id:
            _pending[task_id] = {"chat_id": chat_id,
                                  "started_at": datetime.now(timezone.utc).timestamp()}
            await _send_wa(chat_id,
                f"⏳ Queued  [{task_id[:8]}]\n"
                f"Machine: {machine}  Agent: {agent}\n"
                f"I'll message you when it's done.")
        else:
            await _send_wa(chat_id,
                "❌ Could not reach the queue.\nIs the orchestrator running on MacBook?")

    elif cmd == "run":
        agent  = kwargs["agent"]
        prompt = kwargs["prompt"]
        payload = {"agent": agent, "prompt": prompt, "_target_machine": "mac-mini"}
        task_id = await _create_task("agent_run", payload, notes=prompt[:80])
        if task_id:
            _pending[task_id] = {"chat_id": chat_id,
                                  "started_at": datetime.now(timezone.utc).timestamp()}
            await _send_wa(chat_id, f"⏳ Running on mac-mini / {agent}  [{task_id[:8]}]")
        else:
            await _send_wa(chat_id, "❌ Could not reach the queue.")

    elif cmd == "help":
        await _send_wa(chat_id, (
            "Commands:\n"
            "  status — machine health\n"
            "  queue — active tasks\n"
            "  review — tasks needing input\n"
            "  failures — failed tasks\n"
            "  run <agent> <prompt>\n"
            "  assign <task> [--machine=X] [--agent=Y] [--type=Z]\n\n"
            "Examples:\n"
            "  run claude explain Riverpod\n"
            "  assign refactor auth --machine=thinkpad --agent=claude\n"
            "  assign build iOS release --machine=mac-mini --type=ios_build"
        ))

    else:
        await _send_wa(chat_id,
            f'❌ Unknown: "{body[:40]}"\nSend help for commands.')

    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok", "pending_tasks": len(_pending)}


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("BRIDGE_PORT", "3001"))
    print(f"WhatsApp bridge starting on port {port}")
    print(f"Orchestrator: {ORCHESTRATOR_URL}")
    print(f"Waha:         {WAHA_URL}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
