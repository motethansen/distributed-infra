"""Create and maintain a sprint file (`<project>/sprint-NN.md`) for a sprint the fleet runs.

The conventions give each file one writer: sprint files belong to the project orchestrator, and for a sprint driven by
`dis-agent` that is this module. So it only ever edits a file whose front matter says `owner: dis-agent`; a hand-written
sprint file (owner project-orchestrator, or none) is refused, never rewritten. Status changes reuse backlog_edit, so a
story is never marked done here without the closing flag, and "done" still only comes from the human's sprint review.

Every change records today's remaining points in the burndown table, which is what the Vantage burndown and velocity read.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from shared import backlog_edit, scrum

OWNER = "dis-agent"
_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)


class SprintError(ValueError):
    pass


def sprint_file(project: str, number: int) -> str:
    return f"{project}/sprint-{number:02d}.md"


def short_goal(text: str, limit: int = 200) -> str:
    """The first sentence of the request, or a word-boundary cut with an ellipsis: never a word chopped in half."""
    t = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    if m and len(m.group(1)) <= limit:
        return m.group(1)
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def new_sprint(project: str, number: int, goal: str, day: str) -> str:
    goal = short_goal(goal) or f"Sprint {number}"
    return f"""---
project: {project}
sprint: {number:02d}
started: {day}
goal: {json.dumps(goal, ensure_ascii=False)}
owner: {OWNER}
status: open
updated: {day}
---

# {project} — Sprint {number:02d}

**Goal.** {goal}

> Maintained by `dis-agent` from the plans you approve. It records the stories, their points and the daily burndown.
> A story becomes ✅ done only after your sprint-review confirmation (`dis-agent close`).

## Stories

| Story | Points | Assignee | Status |
|---|---|---|---|

## Burndown

| Date | Remaining points |
|---|---|
"""


def owner_of(text: str) -> str | None:
    m = _FM.match(text)
    if not m:
        return None
    mo = re.search(r"^owner:\s*(\S+)", m.group(1), re.M)
    return mo.group(1) if mo else None


def check_owned(text: str, rel: str) -> None:
    if owner_of(text) != OWNER:
        raise SprintError(f"{rel} is not a dis-agent sprint (owner is {owner_of(text)!r}); use another sprint label or number")


def _stories_table(lines: list[str]):
    for hi, first, end, header, _h in backlog_edit._tables(lines):
        if backlog_edit._col(header, "story") == 0 and backlog_edit._col(header, "points") is not None:
            return hi, first, end
    raise SprintError("no Stories table in the sprint file")


def add_story(text: str, bid: str, title: str, points: float, assignee: str = "fleet", status: str = backlog_edit.PLANNED) -> str:
    check_owned(text, "sprint file")
    lines = text.split("\n")
    _hi, first, end = _stories_table(lines)
    for n in range(first, end):
        if re.search(rf"\b{re.escape(bid)}\b", backlog_edit._split(lines[n])[0]):
            return text                                                   # already there: approving twice changes nothing
    pts = f"{points:g}" if points else "1"
    lines.insert(end, backlog_edit._join([f"{bid} {title.replace('|', '/')[:120]}", pts, assignee, status]))
    return "\n".join(lines)


def _bump(text: str, day: str, status: str | None = None) -> str:
    def fm(m):
        body = re.sub(r"^updated:.*$", f"updated: {day}", m.group(1), flags=re.M)
        if status:
            body = re.sub(r"^status:.*$", f"status: {status}", body, flags=re.M)
        return f"---\n{body}\n---\n"
    return _FM.sub(fm, text, count=1)


def set_story_status(text: str, bid: str, status: str, day: str, closing: bool = False) -> str:
    check_owned(text, "sprint file")
    try:
        text = backlog_edit.set_status(text, bid, status, closing=closing)
    except backlog_edit.BacklogError as e:
        raise SprintError(str(e)) from e
    return _bump(text, day)


def remaining_points(text: str) -> float:
    return scrum.parse_sprint(text, {})["points_remaining"]


def record_burndown(text: str, day: str) -> str:
    """Set today's remaining points in the burndown table (one row per day), and close the sprint line when nothing is left."""
    check_owned(text, "sprint file")
    rem = remaining_points(text)
    lines = text.split("\n")
    for hi, first, end, header, _h in backlog_edit._tables(lines):
        if header and header[0].lower().startswith("date") and any("remaining" in h.lower() for h in header):
            row = backlog_edit._join([day, f"{rem:g}"])
            for n in range(first, end):
                if backlog_edit._split(lines[n])[0] == day:
                    lines[n] = row
                    break
            else:
                lines.insert(end, row)
            break
    else:
        raise SprintError("no Burndown table in the sprint file")
    out = "\n".join(lines)
    total = scrum.parse_sprint(out, {})["points_total"]
    status = f"complete {day} — every story closed after sprint review" if rem == 0 and total else None
    return _bump(out, day, status)


def number_for(repo: Path, project: str, label: str, mapping: dict[str, int]) -> int:
    """A sprint label (S-roles, V1) or a number. Numbers are used as given; a new label takes the next free number."""
    if re.fullmatch(r"\d+", label.strip()):
        return int(label)
    if label in mapping:
        return mapping[label]
    used = [int(m.group(1)) for f in (repo / project).glob("sprint-*.md") for m in [re.match(r"sprint-0*(\d+)\.md$", f.name)] if m]
    return max(used + list(mapping.values()), default=0) + 1
