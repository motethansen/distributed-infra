"""DI-080: `dis-agent go` ends in 'in review' when every plan task finished, even if dispatch raised afterwards."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
import pytest

from shared import scrum_flow as sf
from tests.test_scrum_flow import Queue, _plan, _t

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dis_agent as da  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Never depend on a developer's .env or the real environment: scrum_cli.client() reads SECRET_KEY from the
    environment first (a .env only fills gaps), so a throwaway value here keeps these tests hermetic."""
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ORCHESTRATOR_URLS", "http://localhost:1")


class FleetQueue(Queue):
    """The scrum-flow fake plus GET /tasks (the list), with switches for the failures DI-080 is about."""

    def __init__(self, *a, list_down=False, cut_after=None, **kw):
        super().__init__(*a, **kw)
        self.list_down, self.cut_after, self.posts = list_down, cut_after, 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/tasks":
            return httpx.Response(503) if self.list_down else httpx.Response(200, json=list(self.tasks.values()))
        if req.method == "POST" and req.url.path == "/tasks":
            self.posts += 1
            if self.cut_after is not None and self.posts > self.cut_after:
                return httpx.Response(503)       # the orchestrator stopped answering
        return super().handler(req)


@pytest.fixture
def env(tmp_path, monkeypatch):
    marks, sprint_marks = [], []
    monkeypatch.setattr(da, "projects_cfg", lambda: {"machines": {}, "projects": {}})
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path)
    monkeypatch.setattr(da, "_report", lambda what, r: None)
    monkeypatch.setattr(da, "_sprint_step", lambda what, fn: sprint_marks.append(what))
    monkeypatch.setattr(sf, "mark_story", lambda repo, project, bid, status, note: marks.append((bid, status, note)) or {"pushed": False})
    monkeypatch.setattr(sf, "story_for_plan", lambda plan: plan["story"])

    def go(q, tasks):
        monkeypatch.setattr(da.sc, "client", q.client)
        plan = {"project": "demo", "sprint": "S1", "backlog_id": "DM-1", "story": {"id": "DM-1", "title": "T", "acceptance": ["x"]},
                "tasks": tasks}
        p = tmp_path / "plan.json"
        p.write_text(json.dumps(plan))
        sf.approve_plan(p, "me", "now")
        return da.cmd_go(argparse.Namespace(plan=str(p), approved=False, cwd=None)), p

    return go, marks


def _statuses(marks):
    return [(s, n) for _, s, n in marks]


def test_normal_path_is_unchanged(env, capsys):
    go, marks = env
    _, p = go(FleetQueue(), [_t("a"), _t("b", deps=["a"])])
    assert [s for s, _ in _statuses(marks)] == ["🏃 in progress", "🔍 in review"]
    assert marks[1][2].startswith("work finished; PRs none found; validation: ") and marks[1][2].endswith("; awaiting sprint review")
    assert "not run" not in marks[1][2]
    assert p.with_suffix(".run.json").exists()
    assert "Next: dis-agent review" in capsys.readouterr().out


def test_validate_raises_after_all_tasks_done_goes_to_review(env, monkeypatch, capsys):
    go, marks = env
    monkeypatch.setattr(sf, "parse_validation", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tester timed out")))
    with pytest.raises(SystemExit) as ex:
        _, p = go(FleetQueue(), [_t("a"), _t("b", deps=["a"])])
    assert ex.value.code == 1
    assert marks[-1][1] == "🔍 in review"
    assert "validation: not run: RuntimeError: tester timed out" in marks[-1][2] and marks[-1][2].startswith("work finished; PRs ")
    out = capsys.readouterr().out
    assert "set to 'in review'" in out


def test_orchestrator_unreachable_after_last_task_goes_to_review(env, tmp_path):
    go, marks = env
    with pytest.raises(SystemExit):
        go(FleetQueue(cut_after=2), [_t("a"), _t("b", deps=["a"])])   # both dev tasks queued, then validate/review cannot be
    assert marks[-1][1] == "🔍 in review" and "validation: not run: PlanError: no orchestrator reachable" in marks[-1][2]
    log = json.loads((tmp_path / "plan.run.json").read_text())
    assert [x["key"] for x in log] == ["a", "b"] and all(x["status"] == "done" for x in log)


def test_a_task_not_done_stays_in_progress_and_names_it(env, capsys):
    go, marks = env
    with pytest.raises(SystemExit) as ex:
        go(FleetQueue(fail_keys={"b"}), [_t("a"), _t("b", deps=["a"]), _t("c", deps=["b"])])
    assert ex.value.code == 1
    assert [s for _, s, _ in marks] == ["🏃 in progress"]
    out = capsys.readouterr().out
    assert "not done: b, c." in out and "dis-agent go " in out and "redoes t1.." in out


def test_fleet_unreachable_during_classification_stays_in_progress(env, capsys):
    go, marks = env
    with pytest.raises(SystemExit) as ex:
        go(FleetQueue(cut_after=1, list_down=True), [_t("a"), _t("b", deps=["a"])])
    assert ex.value.code == 1
    assert [s for _, s, _ in marks] == ["🏃 in progress"]
    assert "could not check the fleet" in capsys.readouterr().out
