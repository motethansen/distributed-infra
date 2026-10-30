"""Who ran a task, on what, and what it cost — recorded on the task row itself.

The queue DB (rqlite, replicated to every orchestrator) is the single store, so the MacBook,
thinkpad and mac-mini all see the same history and losing any one of them loses nothing.

Agents report `usage` in their result: {tokens_in, tokens_out, tokens_total, cost_usd}.
Subscription CLIs (claude, codex, agy) are not billed per token, so `cost_usd` is the CLI's own
estimate (Claude) or absent. Nothing here can see how much of a subscription quota is left.
"""
from __future__ import annotations

from typing import Any

# Added to the tasks table by ensure_schema (ALTER TABLE ... ADD COLUMN, idempotent).
TRACK_COLUMNS: dict[str, str] = {
    "agent": "TEXT",          # the agent that actually answered (after any failover)
    "model": "TEXT",
    "tokens_in": "INTEGER",
    "tokens_out": "INTEGER",
    "tokens_total": "INTEGER",
    "cost_usd": "REAL",
    "started_at": "TEXT",     # first time the task went in_progress
    "finished_at": "TEXT",    # last time it reached done/failed/needs_human
    "project": "TEXT",        # from the payload, so sprints/projects can be summed in SQL
    "sprint": "TEXT",
    "story": "TEXT",
}
TERMINAL = ("done", "failed", "needs_human")


def _int(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def from_result(result: Any) -> dict[str, Any]:
    """agent/model/token columns from a task result; only keys that are actually known."""
    if not isinstance(result, dict):
        return {}
    out: dict[str, Any] = {}
    # content/social wrap another agent and rename `agent`; the one that answered is `answered_by`.
    real_agent = result.get("answered_by") or result.get("agent")
    for k, v in (("agent", real_agent), ("model", result.get("model"))):
        if isinstance(v, str) and v:
            out[k] = v[:80]
    u = result.get("usage")
    if isinstance(u, dict):
        tin, tout, ttot = _int(u.get("tokens_in")), _int(u.get("tokens_out")), _int(u.get("tokens_total"))
        if ttot is None and tin is not None and tout is not None:
            ttot = tin + tout
        for k, v in (("tokens_in", tin), ("tokens_out", tout), ("tokens_total", ttot)):
            if v is not None:
                out[k] = v
        try:
            if u.get("cost_usd") is not None:
                out["cost_usd"] = round(float(u["cost_usd"]), 6)
        except (TypeError, ValueError):
            pass
    return out


def from_payload(payload: Any) -> dict[str, Any]:
    """project / sprint / story tags a caller put on the task payload."""
    if not isinstance(payload, dict):
        return {}
    return {k: str(payload[k])[:120] for k in ("project", "sprint", "story") if payload.get(k) not in (None, "")}


def summarise(rows: list[dict[str, Any]], group_by: str) -> list[dict[str, Any]]:
    """Group task rows and total them. group_by: agent | machine | project | sprint | day | type."""
    keyfn = {
        "agent": lambda r: r.get("agent") or "(none)",
        "machine": lambda r: r.get("assigned_to") or "(none)",
        "project": lambda r: r.get("project") or "(untagged)",
        "sprint": lambda r: r.get("sprint") or "(untagged)",
        "type": lambda r: r.get("type") or "(none)",
        "day": lambda r: (r.get("created_at") or "")[:10] or "(none)",
        "story": lambda r: r.get("story") or "(untagged)",
        "all": lambda r: "all",
    }[group_by]
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault(keyfn(r), {"key": keyfn(r), "tasks": 0, "done": 0, "failed": 0, "needs_human": 0,
                                          "open": 0, "tokens_in": 0, "tokens_out": 0, "tokens_total": 0,
                                          "cost_usd": 0.0, "with_usage": 0, "agents": set(), "machines": set(),
                                          "stories": set(), "_secs": 0.0, "_timed": 0})
        g["tasks"] += 1
        st = r.get("status")
        g[st if st in ("done", "failed", "needs_human") else "open"] += 1
        for k in ("tokens_in", "tokens_out", "tokens_total"):
            g[k] += r.get(k) or 0
        g["cost_usd"] += r.get("cost_usd") or 0.0
        g["with_usage"] += 1 if r.get("tokens_total") is not None or r.get("tokens_in") is not None else 0
        if r.get("agent"):
            g["agents"].add(r["agent"])
        if r.get("assigned_to"):
            g["machines"].add(r["assigned_to"])
        if r.get("story"):
            g["stories"].add(r["story"])
        s, f = r.get("started_at"), r.get("finished_at")
        if s and f:
            try:
                from datetime import datetime
                g["_secs"] += max(0.0, (datetime.fromisoformat(f) - datetime.fromisoformat(s)).total_seconds())
                g["_timed"] += 1
            except ValueError:
                pass
    out = []
    for g in groups.values():
        timed = g.pop("_timed")
        secs = g.pop("_secs")
        g["avg_seconds"] = round(secs / timed, 1) if timed else None
        g["cost_usd"] = round(g["cost_usd"], 4)
        g["agents"], g["machines"], g["stories"] = sorted(g["agents"]), sorted(g["machines"]), sorted(g["stories"])
        g["agent_count"], g["machine_count"] = len(g["agents"]), len(g["machines"])
        out.append(g)
    return sorted(out, key=lambda g: (-g["tasks"], g["key"]))
