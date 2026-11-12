"""Stories that look stuck 'in progress' (DI-080): in progress for more than 24 hours with no fleet task running or queued.

Pure functions; the fleet, the clock and the git history are injected so tests use fakes."""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

STUCK_AFTER = timedelta(hours=24)
_ACTIVE_NOT = {"done", "failed", "cancelled", "canceled", "error", "timeout", "timed_out", "dead", "skipped"}
_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _is_active(task: dict) -> bool:
    return str(task.get("status") or "").lower() not in _ACTIVE_NOT


def fleet_state(tasks: list[dict] | None) -> tuple[set[tuple[str, str]], dict[tuple[str, str], dict]]:
    """(running_keys, last_task) from the fleet's task rows; keys are (project, payload story id)."""
    running: set[tuple[str, str]] = set()
    last: dict[tuple[str, str], dict] = {}
    for t in tasks or []:
        p = t.get("payload") or {}
        if not p.get("project") or not p.get("story"):
            continue
        k = (p["project"], str(p["story"]))
        if _is_active(t):
            running.add(k)
        if k not in last or (t.get("created_at") or "") > (last[k].get("created_at") or ""):
            last[k] = t
    return running, last


def git_age_fn(repo: Path, run=subprocess.run) -> Callable[[str, str], datetime | None]:
    """When the backlog row last changed to 'in progress', from the git history of the backlog file (best effort)."""
    def age(project: str, bid: str) -> datetime | None:
        try:
            r = run(["git", "-C", str(repo), "log", "-1", "--format=%cI", "-G", rf"{re.escape(bid)}\b.*in progress",
                     "--", f"{project}/product-backlog.md"], capture_output=True, text=True, timeout=30)
            s = (r.stdout or "").strip()
            return datetime.fromisoformat(s) if r.returncode == 0 and s else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return age


def stuck_in_progress(rows: list[dict], fleet_tasks: list[dict] | None, now: datetime,
                      git_age: Callable[[str, str], datetime | None] | None = None,
                      plans: list[dict] | None = None, fleet_reachable: bool = True) -> dict:
    """rows: [{project, id, title, status, date}] (status 'in_progress' rows are considered).
    plans: [{project, backlog_id, story_id, path}] to map a backlog id to the fleet's story id and to the plan file.
    Returns {"fleet_checked": bool, "could_not_check_fleet": bool, "stuck": [...]}; nothing is listed as stuck when the fleet
    could not be reached (a story with live tasks must never be called stuck)."""
    if not fleet_reachable or fleet_tasks is None:
        return {"fleet_checked": False, "could_not_check_fleet": True, "stuck": []}
    running, last = fleet_state(fleet_tasks)
    by_bid = {(p["project"], p.get("backlog_id")): p for p in plans or [] if p.get("backlog_id")}
    out = []
    for r in rows:
        if r.get("status") not in ("in_progress", "in progress"):
            continue
        pn, bid = r["project"], r["id"]
        pl = by_bid.get((pn, bid))
        keys = {(pn, bid)} | ({(pn, str(pl["story_id"]))} if pl and pl.get("story_id") else set())
        if keys & running:
            continue
        since, source = None, None
        if r.get("date"):
            try:
                since, source = datetime.fromisoformat(r["date"]).replace(tzinfo=timezone.utc), "date in the backlog row"
            except ValueError:
                since = None
        if since is None and git_age:
            since = git_age(pn, bid)
            source = "git history of the backlog" if since else None
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        age = now - since if since else None
        if age is not None and age <= STUCK_AFTER:
            continue                                                        # fresh: the run may just be between tasks
        lt = next((last[k] for k in keys if k in last), None)
        out.append({"project": pn, "id": bid, "title": r.get("title", ""), "age": _fmt_age(age), "age_days": None if age is None else age.days,
                    "age_source": source, "last_task": ({"id": lt.get("id"), "status": lt.get("status")} if lt else None),
                    "plan_path": pl.get("path") if pl else None})
    return {"fleet_checked": True, "could_not_check_fleet": False, "stuck": out}


def _fmt_age(age: timedelta | None) -> str:
    if age is None:
        return "age unknown"
    h = int(age.total_seconds() // 3600)
    return f"{h // 24} d {h % 24} h" if h >= 24 else f"{h} h"


def commands(s: dict) -> dict:
    """The three exact commands for one stuck story: accept, re-run (only when a plan file exists), cancel."""
    p, i = s["project"], s["id"]
    return {"accept": f"dis-agent accept -p {p} --story {i} --from-in-progress --pr <merged PR url> --yes"
                      f'   (or --evidence "what you verified" instead of --pr)',
            "rerun": f"dis-agent go {s['plan_path']}" if s.get("plan_path") else None,
            "cancel": f'dis-agent cancel -p {p} --story {i} --reason "<why>" --yes'}


def render(res: dict) -> str:
    if res.get("could_not_check_fleet"):
        return "Stuck stories: could not check the fleet, so none are listed as stuck.\n"
    if not res["stuck"]:
        return ""
    L = [f"{len(res['stuck'])} {'story looks' if len(res['stuck']) == 1 else 'stories look'} stuck in progress "
         "(more than 24 h, no fleet task running or queued):", ""]
    for s in res["stuck"]:
        lt = s["last_task"]
        L.append(f"  {s['project']} {s['id']}  {s['title'][:80]}   [{s['age']}; last task: {(lt['id'] + ' ' + str(lt['status'])) if lt else 'none known'}]")
        c = commands(s)
        L.append(f"      accept:  {c['accept']}")
        L.append(f"      re-run:  {c['rerun'] or '(no plan file for it)'}")
        L.append(f"      cancel:  {c['cancel']}")
    return "\n".join(L) + "\n"
