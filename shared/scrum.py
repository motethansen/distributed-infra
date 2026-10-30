"""Read the Scrum workspace (vizneoprojects: product backlogs + sprint files) into numbers.

The markdown is written by humans and agents, so the parser is tolerant and never invents:
- a story's status comes from the backlog when the backlog knows the ID (it carries the
  evidence), otherwise from the sprint file;
- remaining points = points of stories that are not done/superseded;
- burndown history is only what a sprint file recorded in a "Date | Remaining points" table. We add
  today's computed value as a labelled last point, and never reconstruct past days.
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

STATUS = {"✅": "done", "🟢": "done", "⛔": "superseded", "🏃": "in_progress", "🔍": "in_review", "🚧": "blocked", "🔵": "planned"}
CLOSED = ("done", "superseded")
_ID = re.compile(r"\b([A-Z]{2,6}-\d{1,4}[a-z]?)\b")
_EMOJI = re.compile("|".join(map(re.escape, STATUS)))


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_sep(line: str) -> bool:
    return bool(re.fullmatch(r"\|?[\s:\-|]+\|?", line.strip())) and "-" in line


def _tables(text: str):
    """Yield (header_cells, [row_cells]) for every markdown table."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and _is_sep(lines[i + 1]):
            header, rows, j = _cells(lines[i]), [], i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append(_cells(lines[j]))
                j += 1
            yield header, rows
            i = j
        else:
            i += 1


def _status_in(text: str) -> str | None:
    m = _EMOJI.search(text or "")
    return STATUS[m.group(0)] if m else None


def _front_matter(text: str) -> dict[str, Any]:
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    try:
        d = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return d if isinstance(d, dict) else {}


def parse_backlog(text: str) -> dict[str, str]:
    """{story id: status} from every table row whose first cell is a story ID and that carries a status."""
    out: dict[str, str] = {}
    for header, rows in _tables(text):
        for r in rows:
            if not r:
                continue
            m = _ID.search(re.sub(r"[*_`]", "", r[0]))
            if not m or m.group(1) in out:
                continue
            # the status cell wins; a marker only on the title cell ("✅ **Rotated the key**") is the fallback
            st = next((s for s in (_status_in(c) for c in r[1:]) if s), None) or _status_in(r[0])
            if st:
                out[m.group(1)] = st
    return out


_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def parse_backlog_dates(text: str) -> dict[str, str]:
    """{story id: YYYY-MM-DD} for closed stories whose STATUS cell carries a date ("🟢 done 2026-08-07").
    A date elsewhere in the row (evidence, notes) is not taken: it may be a decision or review date."""
    out: dict[str, str] = {}
    for header, rows in _tables(text):
        for r in rows:
            m = _ID.search(re.sub(r"[*_`]", "", r[0])) if r else None
            if not m or m.group(1) in out:
                continue
            for c in r[1:]:
                if _status_in(c) in CLOSED:
                    d = _DATE.search(c)
                    if d:
                        out[m.group(1)] = d.group(1)
                    break
    return out


def _points(cell: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", cell or "")
    return float(m.group(0)) if m else None


def parse_sprint(text: str, backlog: dict[str, str], filename: str = "") -> dict[str, Any]:
    fm = _front_matter(text)
    if not fm.get("sprint"):
        m = re.search(r"sprint-0*(\d+)", filename)
        if m:
            fm["sprint"] = m.group(1)
    stories: list[dict[str, Any]] = []
    burn: list[dict[str, Any]] = []
    for header, rows in _tables(text):
        low = [h.lower() for h in header]
        if any("story" in h for h in low) and any("point" in h for h in low):
            ip = next(i for i, h in enumerate(low) if "point" in h)
            ia = next((i for i, h in enumerate(low) if "assignee" in h), None)
            ist = next((i for i, h in enumerate(low) if h.startswith("status")), None)
            for r in rows:
                if len(r) <= ip:
                    continue
                name = re.sub(r"[*_`]", "", r[0])
                sid = _ID.search(name)
                sprint_status = (_status_in(r[ist]) if ist is not None and ist < len(r) else None) or _status_in(r[0]) or "planned"
                key = sid.group(1) if sid else None
                status = backlog.get(key, sprint_status) if key else sprint_status
                stories.append({"id": key, "title": _EMOJI.sub("", name).strip(" -—")[:140], "status": status,
                                "sprint_status": sprint_status, "points": _points(r[ip]) or 0.0,
                                "assignee": re.sub(r"[*_`]", "", r[ia]) if ia is not None and ia < len(r) else None,
                                "drift": bool(key in backlog and backlog[key] != sprint_status)})
        elif any(h.startswith("date") for h in low) and any("remaining" in h for h in low):
            ir = next(i for i, h in enumerate(low) if "remaining" in h)
            for r in rows:
                d, p = r[0].strip(), _points(r[ir]) if len(r) > ir else None
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and p is not None:
                    burn.append({"date": d, "remaining": p, "recorded": True})
    total = sum(s["points"] for s in stories)
    remaining = sum(s["points"] for s in stories if s["status"] not in CLOSED)
    remaining_as_written = sum(s["points"] for s in stories if s["sprint_status"] not in CLOSED)
    if burn or stories:
        burn.append({"date": date.today().isoformat(), "remaining": remaining, "recorded": False})
    started = fm.get("started")
    started = started.isoformat() if isinstance(started, (date, datetime)) else (str(started) if started else None)
    return {
        "sprint": str(fm.get("sprint") or ""), "goal": (fm.get("goal") or "")[:300], "started": started,
        "status": str(fm.get("status") or "")[:160], "updated": str(fm.get("updated") or ""),
        "points_total": total, "points_remaining": remaining, "points_done": total - remaining,
        "points_remaining_as_written": remaining_as_written,   # what the sprint file itself says, uncorrected
        "stories": stories, "burndown": burn,
        "drift": [s["id"] for s in stories if s["drift"]],
    }


BULK = 30            # a single commit closing this many stories is an import/restructure, not a day's work
_GIT_CACHE: dict[tuple[str, str], dict[str, str]] = {}


def git_closure_dates(repo: Path, rel: str, head: str) -> dict[str, str]:
    """{story id: YYYY-MM-DD} = the commit date on which the row first appears closed in `rel`.
    This is when the NOTE was committed, which can trail the real completion. Commits that close
    BULK or more stories at once are skipped (their date says nothing about when work finished)."""
    key = (str(repo / rel), head)
    if key in _GIT_CACHE:
        return _GIT_CACHE[key]
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", "--reverse", "--format=@@%cI", "-p", "--no-color", "-U0", "--", rel],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    seen: dict[str, str] = {}
    day, batch = "", {}
    commits: list[tuple[str, dict[str, str]]] = []
    for line in out.splitlines():
        if line.startswith("@@") and not line.startswith("@@ "):
            if day:
                commits.append((day, batch))
            day, batch = line[2:12], {}
        elif line.startswith("+|") and day:
            cells = _cells(line[1:])
            m = _ID.search(re.sub(r"[*_`]", "", cells[0])) if cells else None
            if m and m.group(1) not in batch:
                st = next((x for x in (_status_in(c) for c in cells[1:]) if x), None) or _status_in(cells[0])
                if st in CLOSED:
                    batch[m.group(1)] = day
    if day:
        commits.append((day, batch))
    for day, batch in commits:
        if len(batch) >= BULK:
            seen.update({i: "" for i in batch})            # remember them as closed-by-import
            continue
        for i in batch:
            seen.setdefault(i, day)
    result = {i: d for i, d in seen.items() if d}
    _GIT_CACHE[key] = result
    return result


def trends(backlog: dict[str, str], dates: dict[str, str], sprints: list[dict]) -> dict[str, Any]:
    """Velocity and S-curve inputs, from what the files actually record.

    scurve: cumulative closed stories (and points, where a sprint table gave the story points) by the
    date written in the backlog status cell, against today's scope. Closed stories with no date are
    counted in `undated_closed` and NOT placed on the curve. velocity: points done per sprint."""
    pts: dict[str, float] = {}
    for sp in sprints:
        for st in sp["stories"]:
            if st["id"] and st["points"]:
                pts[st["id"]] = st["points"]
    closed_ids = [i for i, st in backlog.items() if st in CLOSED]
    by_day: dict[str, dict[str, float]] = {}
    for i in closed_ids:
        if i in dates:
            day = by_day.setdefault(dates[i], {"stories": 0, "points": 0.0})
            day["stories"] += 1
            day["points"] += pts.get(i, 0.0)
    curve, cs, cp = [], 0, 0.0
    for day in sorted(by_day):
        cs += by_day[day]["stories"]
        cp += by_day[day]["points"]
        curve.append({"date": day, "closed_stories": cs, "closed_points": cp,
                      "day_stories": by_day[day]["stories"], "day_points": by_day[day]["points"]})
    velocity = []
    for sp in sprints:
        recorded = [b["date"] for b in sp["burndown"] if b.get("recorded")]
        velocity.append({"sprint": sp["sprint"], "ended": (recorded[-1] if recorded else None) or (sp["updated"] or None),
                         "started": sp["started"], "points_done": sp["points_done"], "points_total": sp["points_total"],
                         "stories_done": sum(1 for st in sp["stories"] if st["status"] in CLOSED)})
    return {"scurve": {"scope_stories": len(backlog), "scope_points": sum(pts.get(i, 0.0) for i in backlog) or None,
                       "points_known_for": sum(1 for i in backlog if i in pts), "curve": curve,
                       "dated_closed": sum(1 for i in closed_ids if i in dates),
                       "undated_closed": sum(1 for i in closed_ids if i not in dates)},
            "velocity": velocity}


def _head(root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def load(repo: str | Path) -> dict[str, Any]:
    """All projects: backlog counts + every sprint. Missing/unreadable files are skipped, not fatal."""
    root = Path(os.path.expanduser(str(repo)))
    projects = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        bl_file = d / "product-backlog.md"
        sprints_files = sorted(d.glob("sprint-*.md"))
        if not bl_file.exists() and not sprints_files:
            continue
        try:
            backlog = parse_backlog(bl_file.read_text(encoding="utf-8")) if bl_file.exists() else {}
        except OSError:
            backlog = {}
        counts = {k: 0 for k in ("done", "superseded", "in_progress", "in_review", "blocked", "planned")}
        for st in backlog.values():
            counts[st] += 1
        sprints = []
        for f in sprints_files:
            try:
                s = parse_sprint(f.read_text(encoding="utf-8"), backlog, f.name)
            except OSError:
                continue
            s["file"] = f.name
            sprints.append(s)
        closed = counts["done"] + counts["superseded"]
        try:
            dates = parse_backlog_dates(bl_file.read_text(encoding="utf-8")) if bl_file.exists() else {}
        except OSError:
            dates = {}
        if bl_file.exists():
            head = _head(root)
            for i, d_ in git_closure_dates(root, f"{d.name}/product-backlog.md", head).items():
                dates.setdefault(i, d_)                    # an explicit date in the status cell wins
        projects.append({"project": d.name, "stories": len(backlog), "counts": counts, "closed": closed,
                         "complete_pct": round(100 * closed / len(backlog), 1) if backlog else None,
                         "sprints": sprints, **trends(backlog, dates, sprints)})
    return {"repo": str(root), "projects": projects}
