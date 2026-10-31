"""Edit a product backlog's markdown tables: add a story, move it through planned / in progress / in
review, and close it. The backlog belongs to the human, so every function returns new text and raises
BacklogError rather than guessing: an unknown story, a story that is already closed, or no table with a
Status column is an error, never a silent edit.

Closing (`✅ done <date>`) is the human's decision at a sprint review; nothing here decides it.
"""
from __future__ import annotations

import re
from collections import Counter

_ID = re.compile(r"\b([A-Z]{2,6})-(\d{1,4}[a-z]?)\b")
CLOSED_MARK = ("✅", "⛔", "🟢", "❌")
PLANNED, IN_PROGRESS, IN_REVIEW = "🔵 planned", "🏃 in progress", "🔍 in review"


class BacklogError(ValueError):
    pass


def _split(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _join(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _is_sep(line: str) -> bool:
    return bool(re.fullmatch(r"\|?[\s:\-|]+\|?", line.strip())) and "-" in line


def _tables(lines: list[str]):
    """Yield (header_index, first_row_index, end_index, header_cells, heading) for each table."""
    i, heading = 0, ""
    while i < len(lines):
        if lines[i].startswith("#"):
            heading = lines[i].lstrip("# ").strip()
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and _is_sep(lines[i + 1]):
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            yield i, i + 2, j, _split(lines[i]), heading
            i = j
        else:
            i += 1


def _col(header: list[str], name: str) -> int | None:
    return next((k for k, h in enumerate(header) if h.lower().startswith(name)), None)


def find(text: str, story_id: str) -> tuple[int, list[str], list[str]]:
    lines = text.split("\n")
    hits = []
    for _, first, end, header, _h in _tables(lines):
        if _col(header, "status") is None:
            continue
        for n in range(first, end):
            cells = _split(lines[n])
            m = _ID.search(re.sub(r"[*_`]", "", cells[0])) if cells else None
            if m and f"{m.group(1)}-{m.group(2)}" == story_id:
                hits.append((n, cells, header))
    if not hits:
        raise BacklogError(f"{story_id} is not in the backlog")
    if len(hits) > 1:
        raise BacklogError(f"{story_id} appears in {len(hits)} tables; edit it by hand")
    return hits[0]


def status_of(text: str, story_id: str) -> str:
    n, cells, header = find(text, story_id)
    return cells[_col(header, "status")]


def is_closed_text(status: str) -> bool:
    return any(m in status for m in CLOSED_MARK)


def set_status(text: str, story_id: str, status: str, evidence: str | None = None, *, closing: bool = False) -> str:
    n, cells, header = find(text, story_id)
    sc, ec = _col(header, "status"), _col(header, "evidence")
    if is_closed_text(cells[sc]):
        raise BacklogError(f"{story_id} is already closed ({cells[sc]!r}); reopen it by hand if that is intended")
    if is_closed_text(status) and not closing:
        raise BacklogError("closing needs closing=True: it is the human's decision at a sprint review")
    cells[sc] = status
    if evidence:
        k = ec if ec is not None else len(cells) - 1
        while len(cells) <= k:
            cells.append("")
        cells[k] = (cells[k] + " — " if cells[k] else "") + evidence
    lines = text.split("\n")
    lines[n] = _join(cells)
    return "\n".join(lines)


def prefix_of(text: str) -> str:
    c = Counter(m.group(1) for line in text.split("\n") if line.startswith("|") for m in [_ID.search(line.split("|")[1] if "|" in line else "")] if m)
    if not c:
        raise BacklogError("no story IDs found to take a prefix from")
    return c.most_common(1)[0][0]


def next_id(text: str) -> str:
    p = prefix_of(text)
    nums = [int(m.group(2)) for line in text.split("\n") if line.startswith("|")
            for m in [_ID.search(re.sub(r"[*_`]", "", line.split("|")[1]))] if m and m.group(1) == p and m.group(2).isdigit()]
    return f"{p}-{max(nums, default=0) + 1:03d}"


def add_story(text: str, title: str, priority: str = "Medium", status: str = PLANNED, evidence: str = "") -> tuple[str, str]:
    """Append a story to the open-items table. Returns (new text, its ID)."""
    lines = text.split("\n")
    cand = [t for t in _tables(lines) if _col(t[3], "status") is not None and _col(t[3], "story") is not None]
    open_ones = [t for t in cand if "closed" not in t[4].lower()]
    pick = next((t for t in open_ones if "open" in t[4].lower()), open_ones[0] if open_ones else None)
    if not pick:
        raise BacklogError("no open-items table with Story and Status columns")
    _, first, end, header, _h = pick
    sid = next_id(text)
    row = [""] * len(header)
    row[0], row[_col(header, "story")], row[_col(header, "status")] = sid, title.replace("|", "/"), status
    if _col(header, "priority") is not None:
        row[_col(header, "priority")] = priority
    ec = _col(header, "evidence")
    if ec is not None:
        row[ec] = evidence
    lines.insert(end, _join(row))
    return "\n".join(lines), sid
