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
