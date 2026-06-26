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
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

# ── Config ────────────────────────────────────────────────────────────────────
WAHA_URL         = os.getenv("WAHA_URL", "http://localhost:3000")
WAHA_API_KEY     = os.getenv("WAHA_API_KEY", "")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://100.97.176.37:8000")
SECRET_KEY       = os.getenv("INFRA_SECRET_KEY", "")
WAHA_SESSION     = "default"
POLL_INTERVAL    = 8    # seconds between completion checks
TASK_TIMEOUT     = 360  # seconds before giving up
HELP_MACHINE     = os.getenv("HELP_MACHINE", "mac-mini")  # which worker answers help queries
BRIDGE_PORT      = int(os.getenv("BRIDGE_PORT", "3001"))
# Public base URL of THIS bridge, reachable from your phone over Tailscale — used
# for artifact download links. Default = Mac Mini Tailscale IP; override via env
# (e.g. BRIDGE_PUBLIC_URL=http://mac-mini.tail8bbe59.ts.net:3001) if the IP changes.
BRIDGE_PUBLIC_URL = os.getenv("BRIDGE_PUBLIC_URL", f"http://100.76.214.54:{BRIDGE_PORT}").rstrip("/")
ARTIFACT_URL_TTL = int(os.getenv("ARTIFACT_URL_TTL", "86400"))  # download-link validity (seconds)

# In-memory map: task_id → {chat_id, started_at}
_pending: dict[str, dict] = {}

# The numeric WhatsApp ID of the user (just the digits, no @suffix). Loaded
# from Waha at startup via _ensure_waha_config — anything outside this self-chat
# is ignored, so the bridge never responds in conversations with other people.
_self_number: str = ""


def _digits(jid: str) -> str:
    """Strip @s.whatsapp.net / @c.us / @lid suffixes from a JID."""
    return jid.split("@", 1)[0] if jid else ""

# Prefixes the bridge uses in its own replies — never treat these as commands.
# Waha echoes the bridge's outbound messages back via webhook (fromMe=true),
# so without this filter every reply would loop back as an "unknown" command.
_REPLY_PREFIXES = (
    "✓", "✗", "⏳", "✅", "❌", "🟢", "🔴", "⚪", "👀", "📋", "⏱", "⚙", "▸", "📎",
    "Commands:", "Machines:", "Needs review:", "Failed:",
)

# `agent <llm> <prompt>` launcher (BLI-050). Maps the user-facing LLM keyword to a
# backend agent in the worker's runner (agents/runner.py AGENTS:
# claude, agy, codex, groq, content, social). All are subscription CLI agents,
# so no API keys are needed. Aliases let the user type natural names.
_AGENT_ALIASES = {
    "claude":       "claude",   # Claude Code (`claude -p`)
    "code":         "claude",   # Claude Code is the claude CLI
    "claude-code":  "claude",
    "agy":          "agy",      # Google Antigravity CLI (`agy -p`)
    "antigravity":  "agy",
    "codex":        "codex",    # OpenAI Codex CLI
    "gpt":          "codex",
    "groq":         "groq",
    "content":      "content",  # long-form content agent
    "social":       "social",   # social-post agent
}


def _agent_choices() -> str:
    """User-facing list of accepted agent keywords for help / error replies."""
    return ", ".join(sorted(_AGENT_ALIASES))


# Multi-turn sessions (BLI-050). Only resumable backends keep a conversation; the
# agent's own CLI stores the history (we pass a fixed session id). After an `agent`
# command, any non-command message continues the session until it goes idle or the
# user sends `end`. In-memory only — a bridge restart drops the mapping.
_RESUMABLE_BACKENDS = {"claude"}
SESSION_TTL = int(os.getenv("AGENT_SESSION_TTL", "1800"))  # seconds of idle before a session expires
# chat_id → {agent, llm, session_id, last_active, turns}
_sessions: dict[str, dict] = {}


def _live_session(chat_id: str, now: float) -> dict | None:
    """Return the chat's active session if it hasn't gone idle, else None (and evict)."""
    s = _sessions.get(chat_id)
    if not s:
        return None
    if now - s["last_active"] > SESSION_TTL:
        _sessions.pop(chat_id, None)
        _save_sessions()
        return None
    return s


# Persist sessions to disk so multi-turn survives a bridge restart. The claude CLI
# already stores the conversation; this just keeps the chat→session_id mapping.
_STATE_FILE = os.getenv("BRIDGE_STATE_FILE", os.path.expanduser("~/.whatsapp-bridge-sessions.json"))


def _save_sessions() -> None:
    """Best-effort write of the current sessions map (atomic via temp + replace)."""
    try:
        tmp = f"{_STATE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_sessions, f)
        os.replace(tmp, _STATE_FILE)
    except OSError:
        pass


def _load_sessions() -> None:
    """Load persisted sessions on startup, dropping any already past their TTL."""
    global _sessions
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    now = datetime.now(timezone.utc).timestamp()
    _sessions = {
        cid: s for cid, s in data.items()
        if isinstance(s, dict) and now - s.get("last_active", 0) <= SESSION_TTL
    }


# Idempotency: Waha can deliver the same message more than once (a global
# docker-compose webhook + the session webhook, or `message` + `message.any`),
# which would make every reply fire twice. Track recently-seen message ids.
_SEEN_TTL = 300  # seconds to remember a message id
_seen_msgs: dict[str, float] = {}


def _is_duplicate(msg_id: str, now: float) -> bool:
    """True if this message id was already handled recently; records it otherwise."""
    if not msg_id:
        return False
    for k in [k for k, ts in _seen_msgs.items() if now - ts > _SEEN_TTL]:
        _seen_msgs.pop(k, None)
    if msg_id in _seen_msgs:
        return True
    _seen_msgs[msg_id] = now
    return False


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


MAX_MSG_CHARS = int(os.getenv("MAX_MSG_CHARS", "3500"))


def _split_chunks(text: str, limit: int = MAX_MSG_CHARS) -> list[str]:
    """Split text into pieces <= limit, breaking on line boundaries where possible.
    A single line longer than limit is hard-split."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


async def _send_long(chat_id: str, text: str, limit: int = MAX_MSG_CHARS) -> None:
    """Send text as one or more WhatsApp messages. Continuation parts get a ▸ (k/n)
    marker — ▸ is in _REPLY_PREFIXES so the bridge ignores its own echoed chunks."""
    parts = _split_chunks(text, limit)
    n = len(parts)
    if n == 1:
        await _send_wa(chat_id, parts[0])
        return
    for i, part in enumerate(parts):
        prefix = f"▸ ({i + 1}/{n})\n"
        await _send_wa(chat_id, (part + f"\n▸ (1/{n})") if i == 0 else prefix + part)


# ── Artifact return ─────────────────────────────────────────────────────────
# When an agent writes a file and names its path in the reply, surface it.
# NOTE: the free WAHA NOWEB engine can't send files in chat (sendImage/sendFile
# are Plus-only → HTTP 422), so we report the validated path + size as text. The
# file lives on the Mac Mini (bridge + worker share the filesystem); retrieve it
# there, or upgrade WAHA Plus to deliver the binary in-chat.
_ARTIFACT_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg",
                  "pdf", "csv", "xlsx", "docx", "pptx", "zip",
                  "mp3", "mp4", "wav", "m4a", "mov"}
MAX_ARTIFACTS = int(os.getenv("MAX_ARTIFACTS", "5"))
MAX_ARTIFACT_BYTES = int(os.getenv("MAX_ARTIFACT_BYTES", str(64 * 1024 * 1024)))  # 64 MB
# absolute or home-relative paths ending in a file extension
_PATH_RE = re.compile(r"(?:/|~/)[^\s'\"()<>]+\.([A-Za-z0-9]{1,5})")


def _extract_artifacts(text: str) -> list[str]:
    """Existing artifact files referenced by absolute/~ path in the agent's reply."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _PATH_RE.finditer(text or ""):
        if m.group(1).lower() not in _ARTIFACT_EXTS:
            continue
        path = os.path.expanduser(m.group(0))
        if path in seen:
            continue
        seen.add(path)
        try:
            if os.path.isfile(path) and 0 < os.path.getsize(path) <= MAX_ARTIFACT_BYTES:
                out.append(path)
        except OSError:
            continue
        if len(out) >= MAX_ARTIFACTS:
            break
    return out


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}TB"


# Token → file map for download links. Tokens are unguessable and time-limited, so
# the /artifact endpoint can only serve files we explicitly registered (no path
# traversal / arbitrary file read). In-memory: links reset on a bridge restart.
_artifact_tokens: dict[str, dict] = {}


def _register_artifact(path: str, now: float) -> str:
    """Mint a one-file download token; evict expired tokens first."""
    for t in [t for t, r in _artifact_tokens.items() if r["expires"] < now]:
        _artifact_tokens.pop(t, None)
    token = uuid.uuid4().hex
    _artifact_tokens[token] = {"path": path, "expires": now + ARTIFACT_URL_TTL}
    return token


def _artifacts_note(paths: list[str], now: float) -> str:
    """Summary of created files with tap-to-download Tailscale links."""
    lines = []
    for p in paths:
        try:
            size = f" ({_human_size(os.path.getsize(p))})"
        except OSError:
            size = ""
        url = f"{BRIDGE_PUBLIC_URL}/artifact/{_register_artifact(p, now)}"
        lines.append(f"• {p}{size}\n  ⬇ {url}")
    return "📎 File(s) created on mac-mini (tap to download over Tailscale):\n" + "\n".join(lines)


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
    if tl in ("end", "/end", "reset", "/reset", "new", "/new"):
        return "end_session", {}
    m = re.match(r"^/?help\s+(.+)", t, re.IGNORECASE)
    if m:
        return "help_ai", {"question": m.group(1).strip()}

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

    # write article: <topic>  →  write_article task on mac-mini (content_agent / Claude)
    m = re.match(r"^!?write\s+article:\s*(.+)", t, re.IGNORECASE)
    if m:
        return "write_article", {"prompt": m.group(1).strip()}

    # write post: <topic> [--format=twitter]  →  write_post task on mac-mini (social_agent / Groq)
    m = re.match(r"^!?write\s+post:\s*(.+)", t, re.IGNORECASE)
    if m:
        body   = m.group(1).strip()
        fmt    = re.search(r"--format=(\S+)", body)
        prompt = re.sub(r"--format=\S+", "", body).strip()
        return "write_post", {"prompt": prompt, "format": fmt.group(1) if fmt else "linkedin"}

    # code review: <path>  →  code_review task on mac-mini
    m = re.match(r"^!?code\s+review:\s*(.+)", t, re.IGNORECASE)
    if m:
        body  = m.group(1).strip()
        focus = re.search(r"--focus=(\S+)", body)
        target = re.sub(r"--focus=\S+", "", body).strip()
        return "code_review", {"target": target, "focus": focus.group(1) if focus else ""}

    # agent <llm> <prompt>  →  launch a CLI agent (BLI-050). Single-shot for now;
    # multi-turn sessions are a later increment. `run` (below) stays as an alias.
    if re.match(r"^/?agent(\s+|$)", t, re.IGNORECASE):
        body  = re.sub(r"^/?agent\s*", "", t, flags=re.IGNORECASE).strip()
        parts = body.split(None, 1)
        llm    = parts[0].lower() if parts else ""
        prompt = parts[1] if len(parts) == 2 else ""
        return "agent", {"llm": llm, "prompt": prompt}

    # run <agent> <prompt>  →  agent_run on mac-mini
    if re.match(r"^/?run\s+", t, re.IGNORECASE):
        body  = re.sub(r"^/?run\s+", "", t, flags=re.IGNORECASE)
        parts = body.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("claude", "agy", "codex", "groq", "content", "social"):
            return "run", {"agent": parts[0].lower(), "prompt": parts[1]}
        return "run", {"agent": "claude", "prompt": body}

    # set-location <place>  →  persist last-known location (weather task, no forecast)
    m = re.match(r"^/?set[-\s]?location\s+(.+)", t, re.IGNORECASE)
    if m:
        return "set_location", {"location": m.group(1).strip()}

    # weather [place]  →  weather task on mac-mini; place is optional (last-known)
    m = re.match(r"^/?weather(?:\s+(.+))?$", t, re.IGNORECASE)
    if m:
        return "weather", {"location": (m.group(1) or "").strip()}

    # assist <subcommand> [args]  →  assistant_run on macbook-pro
    if re.match(r"^/?assist(\s+|$)", t, re.IGNORECASE):
        body  = re.sub(r"^/?assist\s*", "", t, flags=re.IGNORECASE).strip()
        parts = body.split(None, 1) if body else []
        sub   = parts[0].lower() if parts else ""
        args  = parts[1] if len(parts) == 2 else ""
        return "assist", {"subcommand": sub, "args": args}

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
            response_text = result.get("response") or result.get("error") or ""

            if status == "done":
                # full answer, chunked across messages if long
                await _send_long(meta["chat_id"],
                    f"✅ Done  [{task_id[:8]}]\n\n{response_text}")
                # surface any files the agent produced and named in its reply
                artifacts = (result.get("artifacts")
                             if isinstance(result.get("artifacts"), list)
                             else _extract_artifacts(response_text))
                artifacts = [os.path.expanduser(str(a)) for a in artifacts[:MAX_ARTIFACTS]]
                artifacts = [a for a in artifacts if os.path.isfile(a)]
                if artifacts:
                    await _send_wa(meta["chat_id"], _artifacts_note(artifacts, now))
                _pending.pop(task_id, None)

            elif status == "failed":
                # errors can be huge/noisy — cap to a single message
                await _send_wa(meta["chat_id"],
                    f"❌ Failed  [{task_id[:8]}]\n{response_text[:MAX_MSG_CHARS]}")
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
    """Make sure the Waha session has the webhook + events the bridge expects,
    and cache the user's own number so we only respond in the self-chat.

    Without the webhook PUT, deleting waha-sessions/ would silently disable
    replies. Without the self-number cache the bridge can't tell a self-chat
    message from an outgoing message to another contact.
    Idempotent: silent no-op when both are already correct.
    """
    global _self_number
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

        session = r.json() or {}
        me      = session.get("me") or {}
        _self_number = _digits(me.get("id", ""))
        if _self_number:
            print(f"[waha-config] self-chat scope locked to {_self_number}", flush=True)

        cfg = session.get("config") or {}
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
    _load_sessions()  # restore multi-turn sessions from before a restart
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

    # Hard scope: only process messages the user sent IN THEIR OWN SELF-CHAT.
    # Without this, every message the user sends to anyone (fromMe=true) would
    # be parsed as a command and replied to — wrecking real conversations.
    if not is_me:
        return Response(status_code=200)

    # Self-heal: the bridge may have started before the WhatsApp session was
    # linked, leaving _self_number empty — which silently drops every self
    # message. Re-read me.id from Waha as soon as a message arrives (the session
    # must be WORKING for that to happen), instead of requiring a manual restart.
    if not _self_number:
        await _ensure_waha_config()

    if not _self_number or _digits(chat_id) != _self_number:
        return Response(status_code=200)

    # Ignore the bridge's own replies (they always start with a known emoji/prefix)
    if any(body.startswith(p) for p in _REPLY_PREFIXES):
        return Response(status_code=200)

    if not body:
        return Response(status_code=200)

    now = datetime.now(timezone.utc).timestamp()

    # Skip duplicate deliveries of the same WhatsApp message (Waha is at-least-once).
    msg_id = msg.get("id") or (key.get("id") if isinstance(key, dict) else "")
    if _is_duplicate(msg_id, now):
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

    elif cmd == "write_article":
        prompt = kwargs["prompt"]
        payload = {"prompt": prompt, "_target_machine": "mac-mini"}
        task_id = await _create_task("write_article", payload, notes=f"article: {prompt[:70]}")
        if task_id:
            _pending[task_id] = {"chat_id": chat_id,
                                  "started_at": datetime.now(timezone.utc).timestamp()}
            await _send_wa(chat_id,
                f"⏳ Writing article  [{task_id[:8]}]\n\"{prompt[:60]}\"\nI'll send the draft when ready.")
        else:
            await _send_wa(chat_id, "❌ Could not reach the queue.")

    elif cmd == "write_post":
        prompt = kwargs["prompt"]
        fmt    = kwargs.get("format", "linkedin")
        payload = {"prompt": prompt, "format": fmt, "_target_machine": "mac-mini"}
        task_id = await _create_task("write_post", payload, notes=f"post/{fmt}: {prompt[:65]}")
        if task_id:
            _pending[task_id] = {"chat_id": chat_id,
                                  "started_at": datetime.now(timezone.utc).timestamp()}
            await _send_wa(chat_id, f"⏳ Writing {fmt} post  [{task_id[:8]}]")
        else:
            await _send_wa(chat_id, "❌ Could not reach the queue.")

    elif cmd == "code_review":
        target = kwargs["target"]
        focus  = kwargs.get("focus", "")
        payload = {"target": target, "focus": focus, "_target_machine": "mac-mini"}
        notes  = f"review: {target}" + (f" focus={focus}" if focus else "")
        task_id = await _create_task("code_review", payload, notes=notes[:80])
        if task_id:
            _pending[task_id] = {"chat_id": chat_id,
                                  "started_at": datetime.now(timezone.utc).timestamp()}
            await _send_wa(chat_id,
                f"⏳ Code review  [{task_id[:8]}]\nTarget: {target}\nI'll send findings when done.")
        else:
            await _send_wa(chat_id, "❌ Could not reach the queue.")

    elif cmd == "agent":
        llm    = kwargs["llm"]
        prompt = kwargs["prompt"]
        backend = _AGENT_ALIASES.get(llm)
        if not llm or backend is None:
            hint = f' (got "{llm}")' if llm else ""
            await _send_wa(chat_id,
                f"❌ Unknown agent{hint}.\n"
                f"Usage: agent <llm> <prompt>\n"
                f"Available: {_agent_choices()}")
            return Response(status_code=200)
        if not prompt:
            await _send_wa(chat_id, f"❌ No prompt.\nUsage: agent {llm} <your request>")
            return Response(status_code=200)

        # Start a fresh session. Resumable backends (claude) keep a conversation
        # the user can continue with plain replies; others run one-shot.
        resumable = backend in _RESUMABLE_BACKENDS
        payload = {"agent": backend, "prompt": prompt, "_target_machine": "mac-mini"}
        if resumable:
            sid = str(uuid.uuid4())
            payload["session_id"] = sid
            payload["resume"] = False
            _sessions[chat_id] = {"agent": backend, "llm": llm, "session_id": sid,
                                  "last_active": now, "turns": 1}
        else:
            _sessions.pop(chat_id, None)  # non-resumable: clear any stale session
        _save_sessions()
        task_id = await _create_task("agent_run", payload, notes=f"agent/{backend}: {prompt[:60]}")
        if task_id:
            _pending[task_id] = {"chat_id": chat_id, "started_at": now}
            tail = "  — just reply to continue (send `end` to stop)" if resumable else ""
            await _send_wa(chat_id, f"⏳ {backend}  [{task_id[:8]}]\n\"{prompt[:60]}\"{tail}")
        else:
            _sessions.pop(chat_id, None)  # rollback session if the queue is unreachable
            _save_sessions()
            await _send_wa(chat_id, "❌ Could not reach the queue.")

    elif cmd == "end_session":
        had = _sessions.pop(chat_id, None)
        _save_sessions()
        await _send_wa(chat_id, "✓ Session ended." if had else "✓ No active session.")

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

    elif cmd == "weather":
        place   = kwargs.get("location", "")
        payload = {"_target_machine": "mac-mini"}
        if place:
            payload["location"] = place
        notes   = f"weather{(' ' + place) if place else ''}"
        task_id = await _create_task("weather", payload, notes=notes[:80])
        if task_id:
            _pending[task_id] = {"chat_id": chat_id,
                                  "started_at": datetime.now(timezone.utc).timestamp()}
            await _send_wa(chat_id, f"⏳ Weather{(' for ' + place) if place else ''}…  [{task_id[:8]}]")
        else:
            await _send_wa(chat_id, "❌ Could not reach the queue.")

    elif cmd == "set_location":
        place   = kwargs["location"]
        payload = {"set_location": place, "_target_machine": "mac-mini"}
        task_id = await _create_task("weather", payload, notes=f"set-location {place}"[:80])
        if task_id:
            _pending[task_id] = {"chat_id": chat_id,
                                  "started_at": datetime.now(timezone.utc).timestamp()}
            await _send_wa(chat_id, f"⏳ Setting location to {place}…  [{task_id[:8]}]")
        else:
            await _send_wa(chat_id, "❌ Could not reach the queue.")

    elif cmd == "assist":
        sub  = kwargs["subcommand"]
        args = kwargs["args"]
        if sub not in ("today", "sync", "status", "plan"):
            await _send_wa(chat_id,
                "❌ Usage: assist <today|sync|status|plan [today|week]>")
            return Response(status_code=200)

        payload = {"subcommand": sub, "args": args, "_target_machine": "macbook-pro"}
        notes   = f"assist {sub}{(' ' + args) if args else ''}"
        task_id = await _create_task("assistant_run", payload, notes=notes[:80])
        if task_id:
            _pending[task_id] = {"chat_id": chat_id,
                                  "started_at": datetime.now(timezone.utc).timestamp()}
            await _send_wa(chat_id, f"⏳ {notes}  [{task_id[:8]}]")
        else:
            await _send_wa(chat_id, "❌ Could not reach the queue.")

    elif cmd == "help_ai":
        question = kwargs["question"]
        machines = await _list_machines()
        machines_summary = "\n".join(
            f"  {m['name']}: caps={m.get('capabilities', [])} agents={m.get('agents', [])}"
            for m in machines
        ) or "  (unavailable)"

        prompt = (
            "You are a CLI assistant for `da`, a distributed AI agent task queue "
            "that runs across a private Tailscale network.\n"
            "The user is asking from WhatsApp. Reply in plain text only — no markdown, "
            "no asterisks, no bullet symbols, no headers. Keep it under 250 words.\n"
            "If they ask how to write a command, show the exact syntax they can send "
            "as a WhatsApp message. If they ask for an assign command, write the full "
            "command so they can copy-paste it.\n\n"
            "WhatsApp command syntax:\n"
            "  write article: <topic>  — long-form draft saved to ~/Articles/\n"
            "  write post: <topic> [--format=twitter]  — LinkedIn or X post (Groq)\n"
            "  code review: <path> [--focus=security]  — repo code review\n"
            "  agent <llm> <prompt>  — launch a CLI agent (claude, code, agy, codex, groq)\n"
            "  run <agent> <prompt>  — run on mac-mini immediately\n"
            "  assign <description> [--machine=X] [--agent=Y] [--type=Z]\n"
            "    task types: agent_run, write_article, write_post, code_review,\n"
            "                run_script, git_pull, ios_build, android_build,\n"
            "                npm_build, test_run, lint, assistant_run\n"
            "    agents: claude, agy, codex, groq, content, social\n"
            "  assist <today|sync|status|plan [today|week]>  — AI assistant\n"
            "  weather [place]  — today's forecast (no place = last location)\n"
            "  set-location <place>  — remember a place for future weather lookups\n"
            "  queue / status / review / failures\n"
            "  help <question>  — ask about commands\n\n"
            "Agent tasks: include everything in the prompt — the agent cannot ask "
            "follow-up questions. Add a file path if output should be saved, e.g. "
            "\"save to ~/Articles/my-post.md\".\n\n"
            f"Current fleet:\n{machines_summary}\n\n"
            f"User question: {question}"
        )

        payload = {
            "agent": "claude",
            "prompt": prompt,
            "_target_machine": HELP_MACHINE,
        }
        task_id = await _create_task("agent_run", payload, notes=f"help: {question[:60]}")
        if task_id:
            _pending[task_id] = {
                "chat_id": chat_id,
                "started_at": datetime.now(timezone.utc).timestamp(),
            }
            await _send_wa(chat_id, f"⏳ Asking Claude…  [{task_id[:8]}]")
        else:
            await _send_wa(chat_id, "❌ Could not reach the queue.")

    elif cmd == "help":
        await _send_long(chat_id, (
            "📋 Commands — send any of these to yourself\n"
            "\n"
            "🤖 RUN AN AI AGENT\n"
            "  agent <llm> <prompt>\n"
            "  LLMs you can use:\n"
            "   • claude (or code) — Claude Code · multi-turn ✓\n"
            "   • agy — Google Antigravity · one-shot\n"
            "   • codex (or gpt) — OpenAI Codex · one-shot\n"
            "   • groq, content, social\n"
            "  Multi-turn (claude only): after `agent claude …`, just reply normally\n"
            "  to continue the same conversation. Send `end` (or `reset`) to stop.\n"
            "  A session also expires after ~30 min idle, and survives a bridge restart.\n"
            "  • Long answers arrive as several ▸(k/n) messages.\n"
            "  • If the agent saves a file and names its path, you get a 📎 note\n"
            "    (the file stays on mac-mini; in-chat file delivery needs WAHA Plus).\n"
            "  • One-shot agents can't ask follow-ups — put everything in the prompt.\n"
            "\n"
            "✍️ CONTENT\n"
            "  write article: <topic>            — long-form draft (Claude)\n"
            "  write post: <topic> [--format=twitter]  — social post (Groq)\n"
            "  code review: <path> [--focus=security]  — repo review\n"
            "\n"
            "🗓 ASSISTANT (on MacBook)\n"
            "  assist <today|sync|status|plan [today|week]>\n"
            "\n"
            "🌤 INFO\n"
            "  weather [place]          — today's forecast (no place = last location)\n"
            "  set-location <place>     — remember a place for future `weather`\n"
            "\n"
            "🖥 FLEET\n"
            "  status · queue · review · failures\n"
            "  assign <task> [--machine=X] [--agent=Y] [--type=Z]\n"
            "  run <agent> <prompt>              — low-level agent_run on mac-mini\n"
            "\n"
            "❓ HELP\n"
            "  help              — this list\n"
            "  help <question>   — ask Claude how to phrase a command\n"
            "\n"
            "Examples\n"
            "  agent claude help me start a new writing project; ask me questions\n"
            "  agent codex explain this error then suggest a fix: <paste>\n"
            "  agent agy review my task list and suggest today's activities\n"
            "  write article: How distributed AI agents change indie dev\n"
            "  code review: ~/Projects/simtrader --focus=security\n"
            "  assist today"
        ))

    else:
        # Not a command: if a multi-turn session is live, treat this as the next
        # turn and resume it; otherwise it's an unknown command.
        sess = _live_session(chat_id, now)
        if sess:
            sess["last_active"] = now
            sess["turns"] += 1
            _save_sessions()
            payload = {"agent": sess["agent"], "prompt": body,
                       "session_id": sess["session_id"], "resume": True,
                       "_target_machine": "mac-mini"}
            task_id = await _create_task("agent_run", payload,
                                         notes=f"agent/{sess['agent']} cont: {body[:50]}")
            if task_id:
                _pending[task_id] = {"chat_id": chat_id, "started_at": now}
                await _send_wa(chat_id, f"⏳ {sess['agent']} (cont·{sess['turns']})  [{task_id[:8]}]")
            else:
                await _send_wa(chat_id, "❌ Could not reach the queue.")
        else:
            await _send_wa(chat_id,
                f'❌ Unknown: "{body[:40]}"\nSend help for commands.')

    return Response(status_code=200)


@app.get("/artifact/{token}")
async def serve_artifact(token: str):
    """Serve a registered artifact file (token-gated, time-limited). Reachable from
    the phone over Tailscale. Only files explicitly registered are served."""
    now = datetime.now(timezone.utc).timestamp()
    rec = _artifact_tokens.get(token)
    if not rec or rec["expires"] < now or not os.path.isfile(rec["path"]):
        return PlainTextResponse("Not found or link expired.", status_code=404)
    return FileResponse(rec["path"], filename=os.path.basename(rec["path"]))


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
