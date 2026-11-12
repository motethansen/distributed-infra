"""DI-080 acceptance tests, written from the story's acceptance criteria only (independent of the implementation tests).

 AC1  the owner can accept/close a story that is 'in progress' with `--from-in-progress` plus evidence (accept and close CLIs, close_story)
 AC2  without the flag, or without evidence, or from any other status, it is refused with a clear message; the normal in-review path is unchanged
 AC3  the board/desk-worker Accept path keeps its gate (it never passes the flag; an 'in progress' row is still refused)
 AC4  `dis-agent go`: a dispatch that raises after every development task finished ends 'in review' (validation not run), exit non-zero
 AC5  `dis-agent go`: a dispatch that raises with a task not done (or the fleet unreachable) stays 'in progress' and says what to do
 AC6  stuck stories (in progress > 24 h, no live fleet task) are listed by `next` and `review` with accept / re-run / cancel commands;
      'in progress' rows carry a date; a fleet that cannot be reached is reported, never read as 'nothing stuck'
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shared import scrum_flow as sf, scrum_next

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dis_agent as da  # noqa: E402
import desk_worker as dw  # noqa: E402

PR = "https://github.com/o/r/pull/9"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- fixtures
def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real local clone (with a bare origin) holding demo/product-backlog.md, so edit_backlog really commits and pushes."""
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_COMMITTER_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    (work / "demo").mkdir()
    (work / "demo" / "product-backlog.md").write_text("# Demo\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "-u", "origin", "HEAD:main")
    monkeypatch.setattr(da, "viz_repo", lambda: work)
    monkeypatch.setattr(da, "_sprint_step", lambda what, fn: None)
    monkeypatch.setattr(sf, "find_sprint_of", lambda *a, **k: None)
    monkeypatch.setattr(sf, "verify_pr_merged", lambda url: {"merged_at": "2026-11-11", "title": "the work"})
    monkeypatch.setattr(da, "_warn_running_fleet_task", lambda *a, **k: None)
    return work


def put(work: Path, status: str, bid="DM-001"):
    (work / "demo" / "product-backlog.md").write_text(
        "# Demo\n\n## Open items\n\n| ID | Story | Priority | Status | Evidence |\n|---|---|---|---|---|\n"
        f"| {bid} | First | High | {status} | |\n")
    _git(work, "add", "-A")
    subprocess.run(["git", "-C", str(work), "commit", "-q", "--allow-empty", "-m", f"seed {status}"], check=True, capture_output=True)
    _git(work, "push", "-q", "origin", "HEAD:main")


def row(work: Path, bid="DM-001") -> str:
    return next(l for l in (work / "demo" / "product-backlog.md").read_text().split("\n") if l.startswith(f"| {bid}"))


def cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["dis-agent", *argv])
    da.main()


IN_PROGRESS = "🏃 in progress 2026-11-09"
OTHER = {"planned": "🔵 planned", "blocked": "🚧 blocked", "cancelled": "❌ cancelled 2026-11-01", "done": "✅ done 2026-11-01"}


# ---------------------------------------------------------------- AC1
def test_ac1_accept_cli_closes_an_in_progress_story_with_pr(repo, monkeypatch, capsys):
    put(repo, IN_PROGRESS)
    cli(monkeypatch, "accept", "-p", "demo", "--story", "DM-001", "--from-in-progress", "--pr", PR, "--yes")
    r = row(repo)
    assert "✅ done" in r and "in progress" not in r.split("|")[4]
    assert "accepted from in progress, run never reached review" in r and PR in r


def test_ac1_accept_cli_closes_with_evidence_only(repo, monkeypatch):
    put(repo, IN_PROGRESS)
    cli(monkeypatch, "accept", "-p", "demo", "--story", "DM-001", "--from-in-progress", "--evidence", "merged and deployed by hand", "--yes")
    r = row(repo)
    assert "✅ done" in r and "merged and deployed by hand" in r


def test_ac1_close_cli_with_the_flag_closes_an_in_progress_story(repo, monkeypatch, tmp_path):
    """`dis-agent close` reaches close_story with the flag; the plan registry and the fleet are faked."""
    put(repo, IN_PROGRESS)
    seen = {}
    monkeypatch.setattr(sf, "close_story", lambda *a, **k: seen.update(k=k, ev=a[5]) or {"pushed": True, "push_error": None})
    base = tmp_path / "data" / "scrum" / "demo" / "S1"
    base.mkdir(parents=True)
    plan = {"project": "demo", "sprint": "S1", "backlog_id": "DM-001", "story": {"id": "S-1", "title": "T", "acceptance": ["x"]}, "tasks": []}
    (base / "S-1.plan.json").write_text(json.dumps(plan))
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(da.sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: []})())
    try:
        cli(monkeypatch, "close", "-p", "demo", "-s", "S1", "--story", "S-1", "--from-in-progress", "--evidence", "checked by hand", "--yes")
    except SystemExit:
        pass
    assert seen.get("k", {}).get("from_in_progress") is True and "checked by hand" in seen["ev"]


def test_ac1_close_story_function(repo):
    put(repo, IN_PROGRESS)
    sf.close_story(repo, "demo", "DM-001", "michael", "2026-11-12", PR + " merged", from_in_progress=True)
    assert "✅ done 2026-11-12" in row(repo)


# ---------------------------------------------------------------- AC2
def test_ac2_without_the_flag_in_progress_is_refused_with_the_old_message(repo, monkeypatch):
    put(repo, IN_PROGRESS)
    with pytest.raises(SystemExit) as ex:
        cli(monkeypatch, "accept", "-p", "demo", "--story", "DM-001", "--pr", PR, "--yes")
    assert "not accepted: DM-001 is not in review; only a story whose work is finished and under review can be closed" in str(ex.value)
    assert "in progress" in row(repo)


def test_ac2_flag_without_evidence_is_refused_by_cli_and_function(repo, monkeypatch):
    put(repo, IN_PROGRESS)
    with pytest.raises(SystemExit) as ex:
        cli(monkeypatch, "accept", "-p", "demo", "--story", "DM-001", "--from-in-progress", "--yes")
    assert "evidence" in str(ex.value)
    with pytest.raises(sf.PlanError, match="evidence"):
        sf.close_story(repo, "demo", "DM-001", "m", "2026-11-12", "   ", from_in_progress=True)
    assert "in progress" in row(repo)


@pytest.mark.parametrize("name,word", [("planned", "planned"), ("blocked", "blocked"), ("cancelled", "cancel"), ("done", "closed")])
def test_ac2_flag_refuses_every_other_status(repo, name, word):
    put(repo, OTHER[name])
    before = row(repo)
    with pytest.raises(sf.PlanError, match=word):
        sf.close_story(repo, "demo", "DM-001", "m", "2026-11-12", PR, from_in_progress=True)
    assert row(repo) == before


def test_ac2_unmerged_pr_is_refused_and_nothing_changes(repo, monkeypatch):
    put(repo, IN_PROGRESS)

    def not_merged(url):
        raise sf.PlanError(f"{url} is not merged")
    monkeypatch.setattr(sf, "verify_pr_merged", not_merged)
    with pytest.raises(SystemExit) as ex:
        cli(monkeypatch, "accept", "-p", "demo", "--story", "DM-001", "--from-in-progress", "--pr", PR, "--yes")
    assert "not merged" in str(ex.value) and "in progress" in row(repo)


def test_ac2_normal_in_review_path_is_unchanged(repo, monkeypatch):
    put(repo, "🔍 in review")
    cli(monkeypatch, "accept", "-p", "demo", "--story", "DM-001", "--pr", PR, "--yes")
    r = row(repo)
    assert "✅ done" in r and "accepted from in progress" not in r and "confirmed by" in r


def test_ac2_the_flag_on_an_in_review_row_points_at_the_normal_command(repo):
    put(repo, "🔍 in review")
    with pytest.raises(sf.PlanError, match="without --from-in-progress"):
        sf.close_story(repo, "demo", "DM-001", "m", "2026-11-12", PR, from_in_progress=True)


# ---------------------------------------------------------------- AC3
def test_ac3_desk_worker_never_passes_the_flag_and_in_progress_stays_refused(repo, monkeypatch, tmp_path):
    cfg = dw.Config("http://orch.invalid", "s", tmp_path, tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    argvs = []

    def fake_run(argv, timeout):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    dw._run_internal({"verb": "accept_confirmed", "project": "demo", "story_id": "DM-001", "evidence": PR}, cfg, fake_run)
    assert argvs and "--from-in-progress" not in argvs[0] and "--pr" in argvs[0] and "--yes" in argvs[0]
    # the very argv the worker builds, run through the real CLI against an 'in progress' row, is refused
    put(repo, IN_PROGRESS)
    with pytest.raises(SystemExit) as ex:
        cli(monkeypatch, *argvs[0][2:])
    assert "is not in review" in str(ex.value) and "in progress" in row(repo)


# ---------------------------------------------------------------- AC4 / AC5: dis-agent go
class Fleet:
    """Fake fleet client: GET /tasks returns the rows we hand it."""
    def __init__(self, rows, down=False):
        self.rows, self.down = rows, down

    def _call(self, method, path, **kw):
        if self.down:
            raise RuntimeError("no orchestrator reachable")
        return self.rows


def trow(key, status, tid=None):
    return {"id": tid or f"id-{key}", "status": status, "agent": "claude", "created_at": f"2026-11-12T10:0{len(key)}:00",
            "payload": {"project": "demo", "sprint": "S1", "story": "S-1", "flow_key": key, "role": "dev_backend"}}


@pytest.fixture
def go(tmp_path, monkeypatch):
    marks = []
    monkeypatch.setattr(da, "projects_cfg", lambda: {"machines": {}, "projects": {}})
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path)
    monkeypatch.setattr(da, "_report", lambda what, r: None)
    monkeypatch.setattr(da, "_sprint_step", lambda what, fn: None)
    monkeypatch.setattr(sf, "mark_story", lambda repo, project, bid, status, note, *a, **k: marks.append((bid, status, note)) or {"pushed": False})
    monkeypatch.setattr(sf, "story_for_plan", lambda plan: plan["story"])

    def dispatch_boom(*a, **k):
        raise RuntimeError("GET /tasks/abc: HTTP 404 Task not found")

    def run(fleet, dispatch=dispatch_boom, keys=("a", "b")):
        monkeypatch.setattr(da.sc, "client", lambda: fleet)
        monkeypatch.setattr(sf, "dispatch", dispatch)
        tasks = [{"key": k, "role": "dev_backend", "depends_on": [] if i == 0 else [keys[i - 1]], "prompt": "do"} for i, k in enumerate(keys)]
        plan = {"project": "demo", "sprint": "S1", "backlog_id": "DM-001", "story": {"id": "S-1", "title": "T", "acceptance": ["x"]}, "tasks": tasks}
        p = tmp_path / "plan.json"
        p.write_text(json.dumps(plan))
        sf.approve_plan(p, "me", "now")
        return da.cmd_go(argparse.Namespace(plan=str(p), approved=False, cwd=None))
    return run, marks


def test_ac4_all_tasks_done_then_dispatch_raises_ends_in_review(go, capsys):
    run, marks = go
    with pytest.raises(SystemExit) as ex:
        run(Fleet([trow("a", "done"), trow("b", "done")]))
    assert ex.value.code not in (0, None)
    assert [s for _, s, _ in marks] == ["🏃 in progress", "🔍 in review"]
    note = marks[-1][2]
    assert "validation: not run" in note and "404" in note
    out = capsys.readouterr().out
    assert "in review" in out and "by hand" in out


def test_ac4_a_retried_task_counts_by_its_latest_row(go):
    run, marks = go
    with pytest.raises(SystemExit):
        run(Fleet([trow("a", "failed", "old"), {**trow("a", "done", "new"), "created_at": "2026-11-12T11:00:00"}, trow("b", "done")]))
    assert marks[-1][1] == "🔍 in review"


def test_ac5_a_task_not_done_keeps_the_story_in_progress(go, capsys):
    run, marks = go
    with pytest.raises(SystemExit) as ex:
        run(Fleet([trow("a", "done"), trow("b", "failed")]))
    assert ex.value.code not in (0, None)
    assert [s for _, s, _ in marks] == ["🏃 in progress"]
    out = capsys.readouterr().out
    assert "stays 'in progress'" in out and "b" in out and "dis-agent go" in out


def test_ac5_a_missing_task_row_keeps_it_in_progress(go):
    run, marks = go
    with pytest.raises(SystemExit):
        run(Fleet([trow("a", "done")]))
    assert [s for _, s, _ in marks] == ["🏃 in progress"]


def test_ac5_fleet_unreachable_keeps_it_in_progress_and_says_so(go, capsys):
    run, marks = go
    with pytest.raises(SystemExit) as ex:
        run(Fleet([], down=True))
    assert ex.value.code not in (0, None)
    assert [s for _, s, _ in marks] == ["🏃 in progress"]
    assert "could not check the fleet" in capsys.readouterr().out


def test_ac5_normal_run_still_ends_in_review_without_a_failure_note(go, capsys):
    run, marks = go
    run(Fleet([]), dispatch=lambda *a, **k: [{"key": "a", "status": "done"}])
    assert [s for _, s, _ in marks] == ["🏃 in progress", "🔍 in review"]
    assert "not run" not in marks[-1][2] and "Next: dis-agent review" in capsys.readouterr().out


# ---------------------------------------------------------------- AC6: dates and the stuck list
def test_ac6_mark_in_progress_writes_a_date_and_parses_back(repo):
    put(repo, "🔵 planned")
    sf.mark_story(repo, "demo", "DM-001", "🏃 in progress", None, run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""), day="2026-11-12")
    assert "🏃 in progress 2026-11-12" in row(repo)
    from shared import scrum
    r = scrum.backlog_rows((repo / "demo" / "product-backlog.md").read_text())[0]
    assert r["status"] == "in_progress" and r["date"] == "2026-11-12"


def _stuck_env(monkeypatch, tmp_path, status_cell, tasks, down=False, plan=True):
    d = tmp_path / "demo"
    d.mkdir(exist_ok=True)
    (d / "product-backlog.md").write_text(
        "# Demo\n\n## Open items\n\n| ID | Story | Priority | Status | Evidence |\n|---|---|---|---|---|\n"
        f"| DM-001 | First story | High | {status_cell} | |\n")
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path)
    monkeypatch.setattr(da, "projects_cfg", lambda: {"machines": {}, "projects": {"demo": {}}})
    monkeypatch.setattr(da, "_local_plans", lambda: [{"project": "demo", "backlog_id": "DM-001", "story_id": "S-1",
                                                      "path": "data/scrum/demo/S1/S-1.plan.json"}] if plan else [])
    monkeypatch.setattr(da.sc, "client", lambda: Fleet(tasks, down=down))


def _old():
    return (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()


def test_ac6_review_lists_a_stuck_story_with_the_three_commands(monkeypatch, tmp_path, capsys):
    _stuck_env(monkeypatch, tmp_path, f"🏃 in progress {_old()}", [{"id": "tid1", "status": "failed", "created_at": "2026-11-09T00:00:00",
                                                                      "payload": {"project": "demo", "story": "S-1"}}])
    monkeypatch.setattr(scrum_next, "review_items", lambda *a, **k: [])
    monkeypatch.setattr(da, "_board", lambda: {})
    da.cmd_review_queue(argparse.Namespace(project=None))
    out = capsys.readouterr().out
    assert "Nothing is waiting for your review." in out
    assert "stuck in progress" in out and "DM-001" in out and "First story" in out and "tid1" in out
    assert "dis-agent accept -p demo --story DM-001 --from-in-progress" in out
    assert "dis-agent go data/scrum/demo/S1/S-1.plan.json" in out
    assert 'dis-agent cancel -p demo --story DM-001 --reason' in out


def test_ac6_no_plan_file_says_so_instead_of_inventing_a_rerun(monkeypatch, tmp_path, capsys):
    _stuck_env(monkeypatch, tmp_path, f"🏃 in progress {_old()}", [], plan=False)
    monkeypatch.setattr(scrum_next, "review_items", lambda *a, **k: [])
    monkeypatch.setattr(da, "_board", lambda: {})
    da.cmd_review_queue(argparse.Namespace(project=None))
    out = capsys.readouterr().out
    assert "DM-001" in out and "no plan file" in out and "dis-agent go" not in out


def test_ac6_next_adds_one_ranked_suggestion(monkeypatch, tmp_path):
    _stuck_env(monkeypatch, tmp_path, f"🏃 in progress {_old()}", [])
    res = da._stuck_result(None)
    sugs = scrum_next.add_stuck([], res, 6)
    text = scrum_next.render(sugs)
    assert len(sugs) == 1 and "1 story looks stuck in progress" in text
    assert "--from-in-progress" in text and "dis-agent cancel" in text and "DM-001" in text


@pytest.mark.parametrize("status", ["running", "queued", "pending"])
def test_ac6_a_live_fleet_task_means_not_stuck(monkeypatch, tmp_path, status):
    _stuck_env(monkeypatch, tmp_path, f"🏃 in progress {_old()}", [{"id": "x", "status": status, "created_at": "2026-11-12T00:00:00",
                                                                     "payload": {"project": "demo", "story": "S-1"}}])
    assert da._stuck_result(None)["stuck"] == []


def test_ac6_under_24_hours_is_not_stuck(monkeypatch, tmp_path):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # a dated cell shows today's date: dates carry no clock, so a same-day mark must never count as 'over 24 h'
    _stuck_env(monkeypatch, tmp_path, f"🏃 in progress {today}", [])
    res = da._stuck_result(None)
    assert [s["id"] for s in res["stuck"] if s["age_days"] and s["age_days"] >= 1] == []


def test_ac6_fleet_unreachable_is_reported_not_read_as_nothing_stuck(monkeypatch, tmp_path, capsys):
    _stuck_env(monkeypatch, tmp_path, f"🏃 in progress {_old()}", [], down=True)
    res = da._stuck_result(None)
    assert res["stuck"] == [] and res["could_not_check_fleet"] is True
    monkeypatch.setattr(scrum_next, "review_items", lambda *a, **k: [])
    monkeypatch.setattr(da, "_board", lambda: {})
    da.cmd_review_queue(argparse.Namespace(project=None))
    assert "could not check the fleet" in capsys.readouterr().out
    assert scrum_next.add_stuck([], res, 6) == []          # `next` stays silent rather than suggesting anything
