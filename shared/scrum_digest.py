"""The scrum-of-scrums digest: one structured picture across every project, built from the board data.

No model is involved. It answers three questions for the human product owner: what is ready for my
review, what needs a decision, and what has stalled, plus the numbers behind them. The same dict feeds
`dis-agent scrum` and Vantage's Scrum page, so they cannot disagree.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

STALL_DAYS = 14
SPRINT_STALE_DAYS = 7


def _days(a: str | None, today: date) -> int | None:
    try:
        return (today - datetime.strptime((a or "")[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return None


def _open_sprint(p: dict) -> dict | None:
    cand = [s for s in p.get("sprints", []) if (s.get("points_remaining") or 0) > 0]
    return max(cand, key=lambda s: (s.get("updated") or s.get("started") or "", str(s.get("sprint")))) if cand else None


_ORDER = {"in_progress": 0, "in_review": 1, "blocked": 2, "planned": 3}
CANCEL_VISIBLE_DAYS = 60


def _num(v) -> float:
    try:
        return float(str(v))
    except ValueError:
        return -1.0


def _cancel_hidden(date_: str | None, today: date) -> bool:
    """A cancelled story stays visible for CANCEL_VISIBLE_DAYS; an undated one stays visible (its age is unknown)."""
    d = _days(date_, today)
    return d is not None and d > CANCEL_VISIBLE_DAYS


def backlog_view(p: dict, today: date | None = None) -> dict[str, Any]:
    """One project's product backlog arranged by sprint, then everything not yet in a sprint.

    Same structure for Vantage and `dis-agent backlog`, so what you read is what the Scrum Master and Product
    Owner agents are working from. Blocked and in-flight stories carry their reason (the backlog's evidence cell)."""
    today = today or date.today()
    items = {i["id"]: i for i in (p.get("backlog_items") or [])}
    seen: set[str] = set()
    sprints, hidden = [], 0
    closed_in_sprints: set[str] = set()
    for sp in sorted(p.get("sprints") or [], key=lambda s: _num(s.get("sprint")), reverse=True):
        stories = []
        for st in sp.get("stories") or []:
            it = items.get(st.get("id") or "")
            if st.get("id"):
                seen.add(st["id"])
            if st.get("status") in ("done", "superseded") and st.get("id"):
                closed_in_sprints.add(st["id"])
            if st.get("status") == "cancelled" and _cancel_hidden((it or {}).get("date"), today):
                hidden += 1
                continue
            stories.append({"id": st.get("id"), "title": st.get("title"), "status": st.get("status"), "points": st.get("points"),
                            "assignee": st.get("assignee"), "priority": (it or {}).get("priority", ""),
                            "reason": (it or {}).get("evidence", "") if st.get("status") in ("blocked", "in_progress", "in_review", "cancelled") else "",
                            "date": (it or {}).get("date")})
        sprints.append({"sprint": sp.get("sprint"), "goal": sp.get("goal"), "started": sp.get("started"), "status": sp.get("status"),
                        "points_total": sp.get("points_total"), "points_done": sp.get("points_done"),
                        "points_remaining": sp.get("points_remaining"), "stories": stories})
    unscheduled = sorted((i for i in items.values() if i["id"] not in seen and i["status"] != "cancelled"),
                         key=lambda i: (_ORDER.get(i["status"], 9), i["id"]))
    cancelled = []
    for i in items.values():
        if i["status"] == "cancelled" and i["id"] not in seen:
            if _cancel_hidden(i.get("date"), today):
                hidden += 1
            else:
                cancelled.append(i)
    return {"sprints": sprints, "unscheduled": unscheduled, "cancelled": cancelled, "cancelled_hidden": hidden,
            "closed_unscheduled": max(0, (p.get("closed") or 0) - len(closed_in_sprints)), "counts": p.get("counts") or {}}


def build(projects: list[dict], today: date | None = None, opus: dict | None = None) -> dict[str, Any]:
    today = today or date.today()
    lines, review, decisions, adjust = [], [], [], []
    for p in projects:
        cn = p.get("counts") or {}
        active_n = (cn.get("in_progress") or 0) + (cn.get("in_review") or 0)
        open_n = (cn.get("planned") or 0) + (cn.get("blocked") or 0)
        curve = (p.get("scurve") or {}).get("curve") or []
        last_closed = curve[-1]["date"] if curve else None
        since = _days(last_closed, today)
        sp = _open_sprint(p)
        vel = [v["points_done"] for v in (p.get("velocity") or []) if v.get("points_total")][-3:]
        lines.append({"project": p["project"], "closed": p.get("closed", 0), "total": p.get("stories", 0), "pct": p.get("complete_pct"),
                      "in_progress": cn.get("in_progress", 0), "in_review": cn.get("in_review", 0), "blocked": cn.get("blocked", 0),
                      "open": open_n, "last_closed": last_closed, "days_since_close": since,
                      "velocity_avg": round(sum(vel) / len(vel), 1) if vel else None,
                      "sprint": ({"sprint": sp["sprint"], "remaining": sp["points_remaining"], "total": sp["points_total"],
                                  "done": sp.get("points_done", 0)} if sp else None)})
        for it in p.get("active_items") or []:
            row = {"project": p["project"], "id": it["id"], "title": it["title"], "evidence": it["evidence"]}
            if it["status"] == "in_review":
                review.append(row)
            elif it["status"] == "blocked":
                why = (it["evidence"] or "").strip()
                decisions.append({**row, "kind": "blocked", "reason": why[:240],
                                  "text": f"{it['id']} is blocked: {it['title']}" + (f" — {why[:200]}" if why else " — no reason recorded in the backlog")})
        if (active_n + open_n) and since is not None and since >= STALL_DAYS:
            decisions.append({"project": p["project"], "kind": "stalled", "text": f"{p['project']}: nothing closed for {since} days with {active_n + open_n} stories still open"})
        if sp:
            age = _days(sp.get("started"), today)
            if (sp.get("points_done") or 0) == 0 and sp.get("points_total"):
                adjust.append(f"{p['project']} sprint {sp['sprint']}: 0 of {sp['points_total']:g} points done; start it or re-plan it")
            elif age is not None and age >= SPRINT_STALE_DAYS and (sp.get("points_remaining") or 0) > 0:
                adjust.append(f"{p['project']} sprint {sp['sprint']}: started {age} days ago with {sp['points_remaining']:g} of {sp['points_total']:g} points left; hold a sprint review or close it")
            if sp.get("drift"):
                adjust.append(f"{p['project']} sprint {sp['sprint']}: {len(sp['drift'])} stories where the sprint file and the backlog disagree")
    if review:
        adjust.insert(0, f"{len(review)} stories are waiting for your sprint review (see the review queue)")
    if opus and opus.get("cap"):
        used = opus.get("spent", 0) / opus["cap"]
        if used >= 0.8:
            adjust.append(f"Opus budget is {used:.0%} used (${opus['spent']:.2f} of ${opus['cap']:.2f}); the Product Owner falls back to Sonnet at the cap")
    lines.sort(key=lambda r: (-r["in_review"], -(r["in_progress"]), r["project"]))
    return {"as_of": today.isoformat(), "review_queue": review, "decisions": decisions, "projects": lines,
            "adjustments": adjust, "opus": opus or None}


def to_markdown(d: dict[str, Any], fleet: str = "") -> str:
    L = [f"# Scrum of scrums — {d['as_of']}", ""]
    if fleet:
        L += [fleet, ""]
    L += [f"## Ready for your review ({len(d['review_queue'])})", ""]
    L += [f"- **{r['project']} {r['id']}** {r['title']}" + (f"\n  {r['evidence'][:240]}" if r["evidence"] else "") for r in d["review_queue"]] or ["- nothing waiting"]
    L += ["", f"## Needs a decision ({len(d['decisions'])})", ""]
    L += [f"- {x['text']}" for x in d["decisions"]] or ["- nothing"]
    L += ["", "## Suggested adjustments", ""] + ([f"- {a}" for a in d["adjustments"]] or ["- none"])
    L += ["", "## Projects", "", "| project | closed | in progress | in review | blocked | open | last closed | velocity | open sprint |", "|---|---|---|---|---|---|---|---|---|"]
    for r in d["projects"]:
        sp = r["sprint"]
        L.append(f"| {r['project']} | {r['closed']}/{r['total']} | {r['in_progress']} | {r['in_review']} | {r['blocked']} | {r['open']} | "
                 f"{r['last_closed'] or '-'} | {r['velocity_avg'] if r['velocity_avg'] is not None else '-'} | "
                 + (f"S{sp['sprint']} {sp['remaining']:g}/{sp['total']:g} left" if sp else "-") + " |")
    return "\n".join(L) + "\n"
