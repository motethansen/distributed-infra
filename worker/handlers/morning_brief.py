"""Handler for `morning_brief` tasks (#15) — one message stitching the daily parts.

Fans out (in parallel) to the already-shipped specialists — weather, market_brief,
calendar, email — and combines them into a single WhatsApp message. The composite
that makes the fleet feel like one assistant.

payload:
  email_limit: int  — top-N recent emails (default 3).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

import httpx

from shared.models import Task

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", "")
_PART_TIMEOUT = 120


def _headers() -> dict:
    return {"x-secret-key": SECRET_KEY, "Content-Type": "application/json"}


async def _run_part(task_type: str, payload: dict) -> str:
    """Enqueue one specialist sub-task and return its response text (or '')."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{ORCHESTRATOR_URL}/tasks", headers=_headers(),
                             json={"type": task_type, "payload": payload, "notes": "morning brief"})
        if r.status_code != 201:
            return ""
        tid = r.json().get("id")
    except httpx.HTTPError:
        return ""

    waited = 0
    while waited < _PART_TIMEOUT:
        await asyncio.sleep(3)
        waited += 3
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                rr = await c.get(f"{ORCHESTRATOR_URL}/tasks/{tid}", headers=_headers())
        except httpx.HTTPError:
            continue
        if rr.status_code != 200:
            continue
        d = rr.json()
        if d.get("status") in ("done", "failed", "needs_human"):
            result = d.get("result") or {}
            return result.get("response") or d.get("notes") or ""
    return ""


async def handle_morning_brief(task: Task) -> dict:
    email_limit = int(task.payload.get("email_limit", 3) or 3)

    weather, market, calendar, email = await asyncio.gather(
        _run_part("weather", {"_target_machine": "mac-mini"}),
        _run_part("market_brief", {}),
        _run_part("calendar", {"_target_machine": "macbook-pro"}),
        _run_part("email_lookup", {"limit": email_limit, "_target_machine": "macbook-pro"}),
    )

    date = datetime.now().strftime("%A, %d %b %Y")
    blocks = [f"☀️ Good morning — {date}"]
    for part in (weather, calendar, market, email):
        if part and part.strip():
            blocks.append(part.strip())
    return {"response": "\n\n".join(blocks)}
