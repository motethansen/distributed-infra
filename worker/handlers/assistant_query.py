"""Handler for `assistant_query` tasks — queries ai_agent_assistant HTTP API over Tailscale.

Any worker on any machine can handle this task type: it makes an HTTP request to
the assistant API rather than requiring the assistant to be installed locally.

Payload schema:
    {
      "query":  "tasks" | "notes" | "note" | "dashboard" | "plan" | "status" | "llm",
      "params": {}   # optional extra params passed as query string / body
    }

Query types:
    tasks      GET /tasks      — open Obsidian tasks (params: include_done, subdirs)
    notes      GET /notes      — vault note index  (params: subdir)
    note       GET /note       — read a note       (params: path — required)
    dashboard  GET /dashboard  — Dashboard.md sections (params: section)
    plan       POST /plan      — generate plan     (params: mode=today|week)
    status     GET /status     — assistant config summary
    llm        GET /llm        — LLM provider status
"""
from __future__ import annotations

import os

import httpx

from shared.models import Task

ASSISTANT_API_URL = os.getenv("ASSISTANT_API_URL", "http://100.97.176.37:7890")
ASSISTANT_API_KEY = os.getenv("ASSISTANT_API_KEY", "")
TIMEOUT_SECS = 60

_GET_ENDPOINTS: dict[str, str] = {
    "tasks":     "/tasks",
    "notes":     "/notes",
    "note":      "/note",
    "dashboard": "/dashboard",
    "status":    "/status",
    "llm":       "/llm",
}
_POST_ENDPOINTS: dict[str, str] = {
    "plan": "/plan",
}
_ALL_QUERIES = set(_GET_ENDPOINTS) | set(_POST_ENDPOINTS)


async def handle_assistant_query(task: Task) -> dict:
    payload = task.payload or {}
    query = (payload.get("query") or "").strip().lower()
    params = payload.get("params") or {}

    if not query:
        return {"error": "payload.query is required", "allowed": sorted(_ALL_QUERIES)}

    if query not in _ALL_QUERIES:
        return {"error": f"Unknown query type: {query!r}", "allowed": sorted(_ALL_QUERIES)}

    headers: dict[str, str] = {}
    if ASSISTANT_API_KEY:
        headers["x-api-key"] = ASSISTANT_API_KEY

    try:
        async with httpx.AsyncClient(base_url=ASSISTANT_API_URL, timeout=TIMEOUT_SECS) as client:
            if query in _POST_ENDPOINTS:
                resp = await client.post(_POST_ENDPOINTS[query], headers=headers, params=params)
            else:
                resp = await client.get(_GET_ENDPOINTS[query], headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return {
            "error": (
                f"Cannot reach assistant API at {ASSISTANT_API_URL}. "
                "Ensure ai_agent_assistant is running: python main.py --api"
            ),
            "needs_human": True,
            "action": f"Start ai_agent_assistant API on MacBook: python main.py --api",
        }
    except httpx.TimeoutException:
        return {"error": f"Assistant API timed out after {TIMEOUT_SECS}s"}
    except httpx.HTTPStatusError as e:
        return {
            "error": f"Assistant API returned {e.response.status_code}",
            "detail": e.response.text[:500],
        }

    return {"query": query, "data": data}
