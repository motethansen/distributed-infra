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
                decisions.append({**row, "kind": "blocked", "text": f"{it['id']} is blocked: {it['title']}"})
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
