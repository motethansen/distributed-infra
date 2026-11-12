"""DI-080: the stuck-in-progress list in `dis-agent next` and `dis-agent review`."""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shared import backlog_edit, scrum, scrum_flow as sf, scrum_next, stuck_stories as ss

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dis_agent as da  # noqa: E402

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
PLANS = [{"project": "demo", "backlog_id": "DM-001", "story_id": "S-1", "path": "data/scrum/demo/sprint-01/S-1.plan.json"}]


def row(bid, date=None, project="demo"):
    return {"project": project, "id": bid, "title": f"title {bid}", "status": "in_progress", "date": date}


def task(story, status, tid="t1", project="demo"):
    return {"id": tid, "status": status, "created_at": "2026-11-10T00:00:00", "payload": {"project": project, "story": story}}


def ids(res):
    return [s["id"] for s in res["stuck"]]


def test_stuck_older_than_24h_with_no_task():
    res = ss.stuck_in_progress([row("DM-001", "2026-11-10")], [task("S-1", "failed")], NOW, plans=PLANS)
    assert ids(res) == ["DM-001"] and res["stuck"][0]["age_source"] == "date in the backlog row"
    assert res["stuck"][0]["last_task"] == {"id": "t1", "status": "failed"}
    assert res["stuck"][0]["plan_path"] == PLANS[0]["path"] and not res["could_not_check_fleet"]


@pytest.mark.parametrize("status", ["running", "queued", "pending"])
def test_running_or_queued_task_is_not_stuck(status):
    assert ss.stuck_in_progress([row("DM-001", "2026-11-01")], [task("S-1", status)], NOW, plans=PLANS)["stuck"] == []


def test_fresh_under_24h_is_not_stuck_from_git_age():
    res = ss.stuck_in_progress([row("DM-001")], [], NOW, git_age=lambda p, b: NOW - timedelta(hours=5), plans=PLANS)
    assert res["stuck"] == []


def test_age_from_git_and_age_unknown():
    old = ss.stuck_in_progress([row("DM-001")], [], NOW, git_age=lambda p, b: NOW - timedelta(days=3))
    assert old["stuck"][0]["age"] == "3 d 0 h" and old["stuck"][0]["age_source"] == "git history of the backlog"
    unk = ss.stuck_in_progress([row("DM-001")], [], NOW, git_age=lambda p, b: None)
    assert ids(unk) == ["DM-001"] and unk["stuck"][0]["age"] == "age unknown"
    assert ids(ss.stuck_in_progress([row("DM-001")], [], NOW)) == ["DM-001"]        # no git function at all


def test_fleet_unreachable_lists_nothing_and_says_so():
    res = ss.stuck_in_progress([row("DM-001", "2026-11-01")], None, NOW, fleet_reachable=False)
    assert res["could_not_check_fleet"] and res["stuck"] == []
    assert "could not check the fleet" in ss.render(res)


def test_git_age_fn_reads_the_backlog_history(tmp_path):
    seen = []
    def fake(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "2026-11-09T10:00:00+00:00\n", "")
    age = ss.git_age_fn(tmp_path, run=fake)("demo", "DM-001")
    assert age == datetime(2026, 11, 9, 10, tzinfo=timezone.utc) and "demo/product-backlog.md" in seen[0]


BACKLOG = """| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| DM-001 | First | High | 🔵 planned | |
| DM-002 | Second | High | 🏃 in progress | |
"""


def test_date_in_cell_round_trip_through_mark_story_and_parsers(tmp_path):
    repo = tmp_path
    (repo / "demo").mkdir()
    (repo / "demo" / "product-backlog.md").write_text(BACKLOG)
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    sf.mark_story(repo, "demo", "DM-001", "🏃 in progress", None, run=run, day="2026-11-12")
    text = (repo / "demo" / "product-backlog.md").read_text()
    assert backlog_edit.leading_status(backlog_edit.status_of(text, "DM-001")) == "🏃 in progress 2026-11-12"
    rows = {r["id"]: r for r in scrum.backlog_rows(text)}
    assert rows["DM-001"]["status"] == "in_progress" and rows["DM-001"]["date"] == "2026-11-12"
    assert rows["DM-002"]["status"] == "in_progress" and rows["DM-002"]["date"] is None      # no date still parses
    assert scrum.parse_backlog(text)["DM-001"] == scrum.parse_backlog(text)["DM-002"] == "in_progress"
    # a date already there is not doubled, and a review row gets none
    sf.mark_story(repo, "demo", "DM-001", "🏃 in progress 2026-11-12", None, run=run, day="2026-12-01")
    assert backlog_edit.status_of((repo / "demo" / "product-backlog.md").read_text(), "DM-001").count("2026") == 1
    sf.mark_story(repo, "demo", "DM-001", "🔍 in review", None, run=run)
    assert backlog_edit.status_of((repo / "demo" / "product-backlog.md").read_text(), "DM-001") == "🔍 in review"
    # the dated row can still be accepted from in progress
    rows = backlog_edit.leading_status(backlog_edit.status_of(BACKLOG.replace("🏃 in progress", "🏃 in progress 2026-11-12"), "DM-002"))
    assert "in progress" in rows.lower()


def _board():
    return {"projects": [], "digest": {"review_queue": [], "decisions": [], "opus": None}}


def test_next_and_review_print_the_exact_accept_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(da, "projects_cfg", lambda: {"projects": {"demo": {}}})
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path)
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "product-backlog.md").write_text(BACKLOG.replace("🏃 in progress", "🏃 in progress 2026-11-01"))
    monkeypatch.setattr(da, "_board", _board)
    monkeypatch.setattr(da, "_local_plans", lambda: [])
    monkeypatch.setattr(da, "_print_responses", lambda *a, **k: None)
    monkeypatch.setattr(da.sc, "client", lambda: type("C", (), {"_call": lambda self, m, path, **k: []})())
    da.cmd_next(argparse.Namespace(project="demo", limit=6, json=False))
    out = capsys.readouterr().out
    assert "1 story looks stuck in progress" in out
    assert "dis-agent accept -p demo --story DM-002 --from-in-progress --pr <merged PR url> --yes" in out
    da.cmd_review_queue(argparse.Namespace(project="demo"))
    out = capsys.readouterr().out
    assert "dis-agent accept -p demo --story DM-002 --from-in-progress --pr <merged PR url> --yes" in out
    assert 'dis-agent cancel -p demo --story DM-002 --reason' in out and "(no plan file for it)" in out


def test_add_stuck_ranks_and_ignores_empty():
    base = [scrum_next._s("demo", 2, "later", "w", "c", "reads only")]
    assert scrum_next.add_stuck(base, {"stuck": []}) == base
    s = {"stuck": [{"project": "demo", "id": "DM-002", "age": "age unknown", "plan_path": "p.plan.json"}]}
    out = scrum_next.add_stuck(base, s)
    assert out[0]["title"] == "1 story looks stuck in progress" and "dis-agent go p.plan.json" in out[0]["command"]
