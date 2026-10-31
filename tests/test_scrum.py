"""Scrum workspace parser + /scrum/sprints."""
from datetime import date

import pytest

from shared import scrum
from shared.models import Task, TaskStatus, TaskType

BACKLOG = """---
project: demo
---
# Demo backlog

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| DM-001 | first | High | ✅ done | PR #1 |
| DM-002 | second | High | 🔵 planned | |
| **DM-003** | third | Low | ⛔ superseded | decided against |
| DM-004 | fourth | Low | 🚧 blocked | waiting |
| not an id | ignore me | | ✅ done | |
"""

SPRINT = """---
project: demo
sprint: 02
started: 2026-08-01
goal: Ship the thing
status: open
---
# Demo sprint

| Story | From backlog | Assignee | Points | Note |
|---|---|---|---|---|
| **DM-001** first ✅ | product-backlog | macbook | 5 | n |
| **DM-002** second 🔵 | product-backlog | thinkpad | 3 | n |
| **DM-004** fourth 🚧 | product-backlog | thinkpad | 2 | n |
| **DM-005** only in the sprint 🏃 | product-backlog | macbook | 8 pts | n |

| Date | Remaining points | Note |
|---|---|---|
| 2026-08-01 | 18 | opened |
| 2026-08-05 | 13 | DM-001 done |
"""


def test_backlog_ids_and_statuses():
    b = scrum.parse_backlog(BACKLOG)
    assert b == {"DM-001": "done", "DM-002": "planned", "DM-003": "superseded", "DM-004": "blocked"}


def test_sprint_points_remaining_and_burndown():
    b = scrum.parse_backlog(BACKLOG)
    s = scrum.parse_sprint(SPRINT, b, "sprint-02.md")
    assert s["sprint"] == "2" and s["started"] == "2026-08-01" and s["goal"] == "Ship the thing"
    assert s["points_total"] == 18 and s["points_remaining"] == 13 and s["points_done"] == 5
    st = {x["id"]: x for x in s["stories"]}
    assert st["DM-005"]["points"] == 8 and st["DM-005"]["status"] == "in_progress"   # "8 pts", sprint-only story
    assert [p["remaining"] for p in s["burndown"]] == [18, 13, 13]
    assert [p["recorded"] for p in s["burndown"]] == [True, True, False]               # today's point is labelled computed
    assert s["burndown"][-1]["date"] == date.today().isoformat()


def test_backlog_overrides_a_stale_sprint_file_and_the_drift_is_reported():
    stale = SPRINT.replace("**DM-002** second 🔵", "**DM-002** second 🔵")  # sprint says planned
    b = {"DM-002": "done", **{k: v for k, v in scrum.parse_backlog(BACKLOG).items() if k != "DM-002"}}
    s = scrum.parse_sprint(stale, b, "sprint-02.md")
    assert {x["id"]: x["status"] for x in s["stories"]}["DM-002"] == "done"
    assert s["drift"] == ["DM-002"]
    assert s["points_remaining"] == 10 and s["points_remaining_as_written"] == 13   # corrected vs as written


def test_sprint_number_falls_back_to_the_filename_and_garbage_is_ignored():
    s = scrum.parse_sprint("no front matter\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", {}, "sprint-07.md")
    assert s["sprint"] == "7" and s["stories"] == [] and s["points_total"] == 0
    assert scrum.parse_backlog("") == {}


def test_load_reads_a_workspace_and_skips_unreadable_or_underscore_dirs(tmp_path):
    (tmp_path / "demo").mkdir(); (tmp_path / "_scrum").mkdir(); (tmp_path / "empty").mkdir()
    (tmp_path / "demo" / "product-backlog.md").write_text(BACKLOG)
    (tmp_path / "demo" / "sprint-02.md").write_text(SPRINT)
    (tmp_path / "_scrum" / "product-backlog.md").write_text(BACKLOG)
    d = scrum.load(tmp_path)
    assert [p["project"] for p in d["projects"]] == ["demo"]
    p = d["projects"][0]
    assert (p["stories"], p["closed"], p["complete_pct"]) == (4, 2, 50.0) and len(p["sprints"]) == 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db")); monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "k")
    repo = tmp_path / "repo"; (repo / "demo").mkdir(parents=True)
    (repo / "demo" / "product-backlog.md").write_text(BACKLOG); (repo / "demo" / "sprint-02.md").write_text(SPRINT)
    monkeypatch.setenv("SCRUM_REPO", str(repo))
    import orchestrator.db as dbm; importlib.reload(dbm)
    import orchestrator.main as m; importlib.reload(m)
    return m


@pytest.mark.asyncio
async def test_endpoint_joins_fleet_work_to_stories_and_shows_origin(api):
    t = await api.db.insert_task(Task(type=TaskType.agent_run, created_by="mac-mini",
                                      payload={"story": "DM-002", "sprint": "2", "project": "demo", "_origin": "flutter-terminal-iOS"}))
    await api.db.update_task(t.id, {"status": TaskStatus.done, "assigned_to": "thinkpad-x1",
                                    "result": {"agent": "codex", "usage": {"tokens_total": 900}}})
    out = await api.scrum_sprints(x_secret_key="k")
    sprint = out["projects"][0]["sprints"][0]
    st = {s["id"]: s for s in sprint["stories"]}
    assert st["DM-002"]["fleet"]["agents"] == ["codex"] and st["DM-002"]["fleet"]["tokens_total"] == 900
    assert st["DM-001"]["fleet"] is None
    assert sprint["agents_involved"] == ["codex"] and sprint["machines_involved"] == ["thinkpad-x1"]
    r = out["recent_tasks"][0]
    assert (r["origin"], r["machine"], r["agent"], r["story"], r["tokens"]) == ("flutter-terminal-iOS", "thinkpad-x1", "codex", "DM-002", 900)
    assert r["via"] == "mac-mini"
    t2 = await api.db.insert_task(Task(type=TaskType.agent_run, created_by="mac-mini", payload={}))
    old = [x for x in (await api.scrum_sprints(x_secret_key="k"))["recent_tasks"] if x["id"] == t2.id[:8]][0]
    assert old["origin"] is None                      # not recorded is not the same as "asked from mac-mini"
    with pytest.raises(Exception):
        await api.scrum_sprints(x_secret_key="bad")


@pytest.mark.asyncio
async def test_create_task_records_the_client_header_as_origin(api):
    from shared.models import TaskCreate
    t = await api.create_task(TaskCreate(type=TaskType.run_script, payload={}), x_secret_key="k", x_client="fleet-mcp@Michaels-MacBook-Pro")
    assert (await api.db.get_task(t.id)).payload["_origin"] == "fleet-mcp@Michaels-MacBook-Pro"
    t2 = await api.create_task(TaskCreate(type=TaskType.run_script, payload={"_origin": "mine"}), x_secret_key="k", x_client="other")
    assert (await api.db.get_task(t2.id)).payload["_origin"] == "mine"     # an explicit origin is never overwritten


def test_a_superseded_story_is_closed_not_remaining():
    """A deliberate decision not to do something closes it as surely as doing it."""
    sprint = SPRINT.replace("| **DM-004** fourth 🚧 |", "| **DM-003** third 🔵 | product-backlog | thinkpad | 4 | n |\n| **DM-004** fourth 🚧 |")
    s = scrum.parse_sprint(sprint, scrum.parse_backlog(BACKLOG), "sprint-02.md")
    assert {x["id"]: x["status"] for x in s["stories"]}["DM-003"] == "superseded"
    assert s["points_total"] == 22 and s["points_remaining"] == 13        # the 4 superseded points are not remaining


@pytest.mark.asyncio
async def test_aliases_are_folded_and_open_only_backlogs_are_flagged(api, tmp_path, monkeypatch):
    cfg = tmp_path / "machines.yaml"
    cfg.write_text("machines:\n  thinkpad-x1:\n    aliases: [thinkpad, michaelhansen-thinkpad-x13-gen-4]\n")
    monkeypatch.setattr(api, "_MACHINES_CONFIG", cfg)
    for name in ("thinkpad-x1", "thinkpad", "michaelhansen-thinkpad-x13-gen-4"):
        t = await api.db.insert_task(Task(type=TaskType.agent_run, created_by="x"))
        await api.db.update_task(t.id, {"status": TaskStatus.done, "assigned_to": name, "result": {"agent": "claude"}})
    out = await api.scrum_usage(group_by="machine", since=None, until=None, project=None, sprint=None, x_secret_key="k")
    assert [g["key"] for g in out["groups"]] == ["thinkpad-x1"] and out["groups"][0]["tasks"] == 3
    (api.SCRUM_REPO and None)
    import pathlib
    repo = pathlib.Path(api.SCRUM_REPO)
    (repo / "vantage").mkdir(); (repo / "vantage" / "product-backlog.md").write_text(BACKLOG)
    api._SCRUM_CACHE["ts"] = 0
    api._OPEN_ONLY_BACKLOGS.add("vantage")                       # the mechanism still works for a project listed here
    sp = await api.scrum_sprints(x_secret_key="k")
    flags = {p["project"]: (p["backlog_open_work_only"], p["complete_pct"]) for p in sp["projects"]}
    assert flags["vantage"] == (True, None) and flags["demo"] == (False, 50.0)


# ── trends: velocity + S-curve inputs ────────────────────────────────────────

def test_green_done_and_a_title_cell_marker_count_as_closed():
    bl = ("| ID | Story | Pri | Status |\n|---|---|---|---|\n"
          "| DI-1 | **Rotated the key** | High | 🟢 done 2026-08-07 |\n"
          "| DI-2 | ✅ **Updated the plists** | High | evidence only |\n"
          "| DI-3 | Plain | High | 🔵 planned |\n")
    from shared import scrum
    assert scrum.parse_backlog(bl) == {"DI-1": "done", "DI-2": "done", "DI-3": "planned"}


def test_dates_come_only_from_the_status_cell_of_closed_rows():
    from shared import scrum
    bl = ("| ID | Story | Status | Evidence |\n|---|---|---|---|\n"
          "| AB-1 | one | ✅ done 2026-08-07 | merged 2026-08-01 |\n"
          "| AB-2 | two | ✅ done | merged 2026-08-02 |\n"
          "| AB-3 | three | 🔵 planned 2026-09-09 | x |\n")
    assert scrum.parse_backlog_dates(bl) == {"AB-1": "2026-08-07"}


def _git(cwd, *a, date=None):
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = date
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, env=env)


def test_git_history_dates_a_story_when_it_first_appears_closed_and_skips_bulk_imports(tmp_path):
    from shared import scrum
    proj = tmp_path / "p"
    proj.mkdir()
    f = proj / "product-backlog.md"
    hdr = "| ID | Story | Status |\n|---|---|---|\n"
    _git(tmp_path, "init", "-q")
    # commit 1: an import of 31 already-closed stories + one open one -> bulk, undated
    f.write_text(hdr + "".join(f"| PQ-{i:02d} | s | ✅ done |\n" for i in range(31)) + "| PQ-90 | open | 🔵 planned |\n")
    _git(tmp_path, "add", "."); _git(tmp_path, "commit", "-qm", "import", date="2026-08-02T10:00:00+00:00")
    # commit 2: PQ-90 closes on 2026-09-05
    f.write_text(f.read_text().replace("| PQ-90 | open | 🔵 planned |", "| PQ-90 | open | ✅ done |"))
    _git(tmp_path, "add", "."); _git(tmp_path, "commit", "-qm", "close", date="2026-09-05T10:00:00+00:00")
    # commit 3: later edit of the same row must not move its date
    f.write_text(f.read_text().replace("| PQ-90 | open |", "| PQ-90 | open, reworded |"))
    _git(tmp_path, "add", "."); _git(tmp_path, "commit", "-qm", "edit", date="2026-09-20T10:00:00+00:00")
    dates = scrum.git_closure_dates(tmp_path, "p/product-backlog.md", "head-1")
    assert dates == {"PQ-90": "2026-09-05"}                       # the 31 imported ones are NOT dated
    # one fewer than the threshold is a real day's work and is dated
    assert scrum.BULK == 30


def test_trends_builds_a_cumulative_curve_and_per_sprint_velocity():
    from shared import scrum
    backlog = {"AB-1": "done", "AB-2": "done", "AB-3": "done", "AB-4": "planned"}
    dates = {"AB-1": "2026-10-01", "AB-2": "2026-10-01", "AB-3": "2026-10-03"}
    sprints = [{"sprint": "1", "started": "2026-10-01", "updated": "2026-10-03", "points_done": 5.0, "points_total": 8.0,
                "burndown": [{"date": "2026-10-02", "remaining": 3, "recorded": True}, {"date": "2026-10-09", "remaining": 3, "recorded": False}],
                "stories": [{"id": "AB-1", "points": 2.0, "status": "done"}, {"id": "AB-2", "points": 3.0, "status": "done"},
                            {"id": "AB-4", "points": 3.0, "status": "planned"}]}]
    t = scrum.trends(backlog, dates, sprints)
    c = t["scurve"]
    assert [(x["date"], x["closed_stories"], x["closed_points"]) for x in c["curve"]] == [("2026-10-01", 2, 5.0), ("2026-10-03", 3, 5.0)]
    assert c["scope_stories"] == 4 and c["scope_points"] == 8.0 and c["points_known_for"] == 3
    assert c["dated_closed"] == 3 and c["undated_closed"] == 0
    assert t["velocity"] == [{"sprint": "1", "ended": "2026-10-02", "started": "2026-10-01", "points_done": 5.0,
                              "points_total": 8.0, "stories_done": 2}]
    t2 = scrum.trends(backlog, {"AB-1": "2026-10-01"}, sprints)
    assert t2["scurve"]["undated_closed"] == 2 and len(t2["scurve"]["curve"]) == 1       # undated are counted, not plotted


def _repo_with(tmp_path, commits):
    """commits: [(date, {path: text})] -> a git repo laid out like vizneoprojects."""
    _git(tmp_path, "init", "-q")
    for date, files in commits:
        for rel, text in files.items():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text(text)
        _git(tmp_path, "add", "."); _git(tmp_path, "commit", "-qm", "c", date=date)


def _rows(n, status="✅ done", start=0):
    return "".join(f"| QQ-{i:02d} | s | {status} |\n" for i in range(start, start + n))


HDR = "| ID | Story | Status |\n|---|---|---|\n"


def test_bulk_threshold_is_exactly_thirty(tmp_path):
    from shared import scrum
    _repo_with(tmp_path, [("2026-08-02T10:00:00+00:00", {"p/product-backlog.md": HDR + _rows(scrum.BULK - 1)})])
    assert len(scrum.git_closure_dates(tmp_path, "p/product-backlog.md", "h-a")) == scrum.BULK - 1
    other = tmp_path / "b"
    other.mkdir()
    _repo_with(other, [("2026-08-02T10:00:00+00:00", {"p/product-backlog.md": HDR + _rows(scrum.BULK)})])
    assert scrum.git_closure_dates(other, "p/product-backlog.md", "h-b") == {}


def test_a_bulk_imported_story_edited_later_stays_undated(tmp_path):
    from shared import scrum
    body = HDR + _rows(scrum.BULK)
    _repo_with(tmp_path, [("2026-08-02T10:00:00+00:00", {"p/product-backlog.md": body}),
                          ("2026-09-09T10:00:00+00:00", {"p/product-backlog.md": body.replace("QQ-00 | s", "QQ-00 | reworded")})])
    assert "QQ-00" not in scrum.git_closure_dates(tmp_path, "p/product-backlog.md", "h-c")


def test_load_prefers_the_date_written_in_the_status_cell_over_the_commit_date(tmp_path):
    from shared import scrum
    text = HDR + "| QQ-01 | s | ✅ done 2026-07-01 |\n| QQ-02 | s | ✅ done |\n"
    _repo_with(tmp_path, [("2026-09-01T10:00:00+00:00", {"proj/product-backlog.md": text})])
    curve = scrum.load(tmp_path)["projects"][0]["scurve"]["curve"]
    assert [(c["date"], c["day_stories"]) for c in curve] == [("2026-07-01", 1), ("2026-09-01", 1)]


# ── the board pulls its own notes repo ───────────────────────────────────────

def _clone_pair(tmp_path):
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    g = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True, env=env)   # noqa: E731
    origin = tmp_path / "o.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    a, b = tmp_path / "a", tmp_path / "b"
    subprocess.run(["git", "clone", "-q", str(origin), str(a)], check=True, capture_output=True)
    (a / "p").mkdir()
    (a / "p" / "product-backlog.md").write_text("| ID | Story | Status |\n|---|---|---|\n| PP-001 | s | 🔵 planned |\n")
    g(a, "add", "."); g(a, "commit", "-qm", "1"); g(a, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(origin), str(b)], check=True, capture_output=True)
    return g, a, b


@pytest.mark.asyncio
async def test_board_fast_forwards_its_repo_at_most_every_interval_and_drops_the_cache(api, tmp_path, monkeypatch):
    g, a, b = _clone_pair(tmp_path)
    monkeypatch.setattr(api, "SCRUM_REPO", str(b))
    api._PULL.update(ts=0.0, ok=None, error=None, at=None)
    first = (await api.scrum_sprints(x_secret_key="k"))["projects"][0]["stories"]
    assert first == 1 and api._PULL["ok"] is True
    (a / "p" / "product-backlog.md").write_text((a / "p" / "product-backlog.md").read_text() + "| PP-002 | t | 🏃 in progress |\n")
    g(a, "commit", "-qam", "2"); g(a, "push", "-q", "origin", "HEAD:main")
    assert (await api.scrum_sprints(x_secret_key="k"))["projects"][0]["stories"] == 1          # inside the interval: no pull yet
    api._PULL["ts"] = 0.0
    sp = await api.scrum_sprints(x_secret_key="k")
    assert sp["projects"][0]["stories"] == 2 and sp["repo"]["pull"]["ok"] is True              # pulled, cache dropped, new row seen


@pytest.mark.asyncio
async def test_a_failed_pull_is_reported_not_fatal_and_can_be_turned_off(api, tmp_path, monkeypatch):
    g, a, b = _clone_pair(tmp_path)
    g(b, "remote", "remove", "origin")
    monkeypatch.setattr(api, "SCRUM_REPO", str(b))
    api._PULL.update(ts=0.0, ok=None, error=None, at=None)
    sp = await api.scrum_sprints(x_secret_key="k")
    assert sp["projects"][0]["stories"] == 1 and sp["repo"]["pull"]["ok"] is False and sp["repo"]["pull"]["error"]
    api._PULL.update(ts=0.0, ok=None, error=None, at=None)
    monkeypatch.setenv("SCRUM_REPO_PULL", "0")
    sp = await api.scrum_sprints(x_secret_key="k")
    assert sp["repo"]["pull"]["ok"] is None                                                     # never attempted


# ── cancelled stories and the per-sprint backlog rows ────────────────────────

CANCEL_BL = ("| ID | Story | Priority | Status | Evidence |\n|---|---|---|---|---|\n"
             "| CC-001 | planned | High | 🔵 planned | |\n"
             "| CC-002 | scoped out | Low | ❌ cancelled 2026-10-20 | not needed since CC-001 covers it |\n"
             "| CC-003 | Stripe cleanup of cancelled subscriptions | Low | 🏃 in progress | the title mentions cancelled |\n"
             "| CC-004 | old | Low | cancelled | undated |\n| CC-005 | done | Low | ✅ done 2026-01-01 | |\n")


def test_cancelled_is_a_status_only_when_the_cell_starts_with_it():
    got = scrum.parse_backlog(CANCEL_BL)
    assert got == {"CC-001": "planned", "CC-002": "cancelled", "CC-003": "in_progress", "CC-004": "cancelled", "CC-005": "done"}


def test_backlog_rows_keep_reasons_and_dates_and_skip_closed():
    rows = {r["id"]: r for r in scrum.backlog_rows(CANCEL_BL)}
    assert set(rows) == {"CC-001", "CC-002", "CC-003", "CC-004"}
    assert rows["CC-002"]["date"] == "2026-10-20" and "covers it" in rows["CC-002"]["evidence"] and rows["CC-002"]["priority"] == "Low"
    assert rows["CC-004"]["date"] is None


def test_cancelled_work_is_neither_scope_nor_remaining_in_a_sprint_or_the_completion_figure(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "product-backlog.md").write_text(CANCEL_BL)
    (tmp_path / "p" / "sprint-01.md").write_text(
        "---\nsprint: 1\n---\n| Story | Points | Status |\n|---|---|---|\n| CC-001 planned | 3 | 🔵 |\n| CC-002 scoped out | 5 | 🔵 |\n| CC-005 done | 2 | ✅ |\n")
    pr = scrum.load(tmp_path)["projects"][0]
    assert pr["counts"]["cancelled"] == 2 and pr["stories"] == 3 and pr["closed"] == 1          # 5 rows, 2 cancelled
    assert pr["complete_pct"] == round(100 * 1 / 3, 1) and pr["scurve"]["scope_stories"] == 3
    sp = pr["sprints"][0]
    assert sp["points_total"] == 5 and sp["points_remaining"] == 3 and sp["points_done"] == 2    # the 5 cancelled points vanish
    assert [s["status"] for s in sp["stories"]] == ["planned", "cancelled", "done"]
