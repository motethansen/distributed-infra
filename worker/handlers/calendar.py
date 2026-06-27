"""Handler for `calendar` tasks (#14) — today's events + next free slot.

Reads the assistant's calendar over its HTTP API (GET /calendar), so any worker
can serve it without a local copy of the assistant. The assistant's calendar is
its local ICS (synced from Obsidian #gcal tags).

payload:
  min_free_minutes: int — size of the "next free slot" to find (default 120).
"""
from __future__ import annotations

import os

import httpx

from shared.models import Task

ASSISTANT_API_URL = os.getenv("ASSISTANT_API_URL", "http://100.97.176.37:7890")
ASSISTANT_API_KEY = os.getenv("ASSISTANT_API_KEY", "")
TIMEOUT_SECS = 30


def _fmt_time(s: str | None) -> str:
    """Pull a HH:MM out of an ISO-ish datetime string; fall back to the raw value."""
    if not s:
        return ""
    if "T" in s:
        return s.split("T", 1)[1][:5]
    if " " in s and ":" in s:
        return s.split(" ", 1)[1][:5]
    return s


def _format(data: dict) -> str:
    events = data.get("events") or []
    lines = [f"🗓 {data.get('date', 'Today')}"]
    if not events:
        lines.append("No events today.")
    else:
        for e in events:
            if e.get("all_day"):
                lines.append(f"• (all day) {e.get('summary', '')}")
            else:
                t = _fmt_time(e.get("start"))
                te = _fmt_time(e.get("end"))
                span = f"{t}–{te}" if te else t
                lines.append(f"• {span}  {e.get('summary', '')}")
    nf = data.get("next_free_slot")
    if nf:
        hrs = nf.get("minutes", 0) / 60
        lines.append(f"\n🟢 Next free: {nf['start']}–{nf['end']} ({hrs:.1f}h)")
    else:
        lines.append("\n🔴 No free slot of the requested length today.")
    return "\n".join(lines)


async def handle_calendar(task: Task) -> dict:
    payload = task.payload or {}
    min_free = int(payload.get("min_free_minutes", 120) or 120)

    headers = {"x-api-key": ASSISTANT_API_KEY} if ASSISTANT_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECS) as c:
            r = await c.get(f"{ASSISTANT_API_URL}/calendar",
                            params={"min_free_minutes": min_free}, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as exc:
        return {"needs_human": True,
                "notes": f"Could not reach the assistant calendar API: {exc}",
                "action": "Is the assistant API running on macbook-pro (:7890)?"}

    return {"response": _format(data), "data": data}
