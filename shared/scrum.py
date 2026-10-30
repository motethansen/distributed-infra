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
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

STATUS = {"✅": "done", "⛔": "superseded", "🏃": "in_progress", "🔍": "in_review", "🚧": "blocked", "🔵": "planned"}
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
            st = next((s for s in (_status_in(c) for c in r[1:]) if s), None)
            if st:
                out[m.group(1)] = st
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
        projects.append({"project": d.name, "stories": len(backlog), "counts": counts, "closed": closed,
                         "complete_pct": round(100 * closed / len(backlog), 1) if backlog else None,
                         "sprints": sprints})
    return {"repo": str(root), "projects": projects}
