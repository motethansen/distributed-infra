"""Concierge / router (#9) — the front door for freeform requests.

Classifies a freeform query (deterministic keywords first, cheap LLM only for the
ambiguous tail via the #5 router), maps it to a specialist, enqueues that
specialist sub-task on the right machine, waits for it, and returns the answer.

This is the Hierarchical Supervisor primitive: a handler that decomposes a request
and drives the queue — no new framework.

payload:
  query: str — the freeform request (e.g. "weather in Tokyo", "any unread email",
               "what's my day", "do I have notes on X").
"""
from __future__ import annotations

import asyncio
import os
import re

import httpx

from shared.models import Task

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", "")
_SUB_TIMEOUT = 90  # seconds to wait for the specialist sub-task

# Deterministic keyword → category. Cheap, zero-LLM for the common cases.
_KEYWORDS: dict[str, list[str]] = {
    "weather":  ["weather", "forecast", "temperature", "rain", "raining", "sunny", "humid", "hot ", "cold "],
    "email":    ["email", "e-mail", "mail", "inbox", "unread", "gmail"],
    "calendar": ["calendar", "schedule", "agenda", "free slot", "free time", "my day", "events", "event",
                 "meeting", "appointment", "busy"],
    "tasks":    ["task", "todo", "to-do", "to do", "obsidian", "note", "notes", "plan", "planning", "logseq"],
    "shop":     ["buy", "shop", "shopping", "price", "grocery", "groceries", "redmart", "lazada",
                 "amazon", "order", "deal", "cheapest"],
}

_CATEGORIES = list(_KEYWORDS) + ["unknown"]


def _classify_keywords(q: str) -> str | None:
    ql = f" {q.lower()} "
    for cat, words in _KEYWORDS.items():
        if any(w in ql for w in words):
            return cat
    return None


async def _classify_llm(q: str) -> str:
    """Fallback classifier — routed to the cheapest model via #5 (task_kind=classify)."""
    from agents.runner import run_agent
    prompt = (
        "Classify the user request into exactly ONE category from this list: "
        "weather, email, calendar, tasks, shop, unknown.\n"
        "Reply with ONLY the single category word, nothing else.\n\n"
        f"Request: {q}"
    )
    res = await run_agent(prompt=prompt, task_kind="classify")
    text = (res.get("response") or "").strip().lower() if res.get("ok") else ""
    for cat in _CATEGORIES:
        if cat in text:
            return cat
    return "unknown"


def _extract_location(q: str) -> str:
    m = re.search(r"\bin\s+(.+)$", q.strip(), re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _build_specialist(category: str, query: str) -> tuple[str, dict] | None:
    """Map a category to (task_type, payload incl. _target_machine)."""
    if category == "weather":
        payload = {"_target_machine": "mac-mini"}
        loc = _extract_location(query)
        if loc:
            payload["location"] = loc
        return "weather", payload
    if category == "email":
        ql = query.lower()
        q = ""
        if "unread" in ql:
            q = "is:unread"
        else:
            m = re.search(r"from\s+(\S+)", ql)
            if m:
                q = f"from:{m.group(1)}"
        return "email_lookup", {"query": q, "limit": 5, "_target_machine": "macbook-pro"}
    if category == "calendar":
        return "calendar", {"_target_machine": "macbook-pro"}
    if category == "tasks":
        sub = "plan" if "plan" in query.lower() else "today"
        return "assistant_run", {"subcommand": sub, "_target_machine": "macbook-pro"}
    return None


def _headers() -> dict:
    return {"x-secret-key": SECRET_KEY, "Content-Type": "application/json"}


async def _enqueue_and_wait(task_type: str, payload: dict) -> dict:
    """Create a specialist sub-task and poll until it finishes."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{ORCHESTRATOR_URL}/tasks", headers=_headers(),
                         json={"type": task_type, "payload": payload, "notes": "via find"})
        if r.status_code != 201:
            return {"error": f"could not enqueue {task_type} ({r.status_code})"}
        tid = r.json().get("id")

    waited = 0
    async with httpx.AsyncClient(timeout=15) as c:
        while waited < _SUB_TIMEOUT:
            await asyncio.sleep(3)
            waited += 3
            rr = await c.get(f"{ORCHESTRATOR_URL}/tasks/{tid}", headers=_headers())
            if rr.status_code != 200:
                continue
            d = rr.json()
            status = d.get("status")
            if status in ("done", "failed", "needs_human"):
                result = d.get("result") or {}
                if status == "done":
                    return {"response": result.get("response", "")}
                return {"error": result.get("error") or d.get("notes") or f"sub-task {status}"}
    return {"error": f"{task_type} timed out"}


async def handle_find(task: Task) -> dict:
    query = (task.payload.get("query") or "").strip()
    if not query:
        return {"response": "What can I find for you? Try: find weather in Tokyo · find unread email · "
                            "find my schedule · find today's tasks."}

    category = _classify_keywords(query) or await _classify_llm(query)

    if category == "shop":
        return {"response": "🛒 Commerce search (Redmart / Lazada / Amazon.sg) is the next track (#10) — "
                            "not wired up yet."}

    spec = _build_specialist(category, query)
    if not spec:
        return {"response": f"I couldn't tell what to look up for \"{query}\".\n"
                            "I can do: weather, email, calendar, tasks/notes. "
                            "Try e.g. `find weather in Tokyo` or `find unread email`."}

    task_type, payload = spec
    result = await _enqueue_and_wait(task_type, payload)
    answer = result.get("response") or result.get("error") or "no result"
    return {"response": f"🔎 ({category})\n{answer}", "category": category}
