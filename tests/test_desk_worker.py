import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location("desk_worker", Path(__file__).resolve().parent.parent / "scripts" / "desk_worker.py")
dw = importlib.util.module_from_spec(_spec)
sys.modules["desk_worker"] = dw
_spec.loader.exec_module(dw)


@pytest.fixture
def cfg(tmp_path):
    return dw.Config("http://orch.invalid", "s", tmp_path, tmp_path / "data")


def seed(cfg, stories, plans=()):
    d = cfg.data_dir / "scrum" / "p" / "s1"
    d.mkdir(parents=True)
    (d / "stories.json").write_text(json.dumps(stories))
    for sid in plans:
        (d / f"{sid}.plan.json").write_text(json.dumps({"tasks": [1, 2], "planned_by": "opus"}))
    return d


STORIES = [{"id": "S-1", "title": "a", "points": 2}, {"id": "S-2", "title": "b", "points": 1},
           {"id": "S-3", "title": "h", "points": 1, "human_only": True}]


def test_d006(tmp_path):
    f = tmp_path / "d.md"
    assert not dw.d006_accepted(f)
    f.write_text("## D-005\nStatus: accepted\n## D-006 caps\nStatus: Proposed\n")
    assert not dw.d006_accepted(f)
    f.write_text("## D-005\nStatus: proposed\n## D-006 caps\nStatus: Accepted\n## D-007\n")
    assert dw.d006_accepted(f)
    f.write_text("## D-006 caps [accepted]\n")
    assert dw.d006_accepted(f)
    f.write_text("## D-006\nStatus: proposed\n## D-007\nStatus: accepted\n")
    assert not dw.d006_accepted(f)


def test_proposal(cfg):
    seed(cfg, STORIES, plans=["S-2"])
    r = dw.proposal_from_dir(cfg.data_dir, "p", "s1")
    assert r["unplanned"] == [{"id": "S-1", "title": "a", "points": 2}] and r["planned"] == ["S-2"]


def test_plan_exists_untouched(cfg):
    d = seed(cfg, STORIES, plans=["S-1"])
    m = (d / "S-1.plan.json").stat().st_mtime_ns
    run = lambda *a, **k: pytest.fail("must not run")
    with pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "plan", "args": {"project": "p", "sprint": "s1", "story_id": "S-1"}}, cfg, run=run)
    assert e.value.code == "plan_exists" and (d / "S-1.plan.json").stat().st_mtime_ns == m


def test_plan_ok_and_argv(cfg):
    d = seed(cfg, STORIES)
    seen = {}

    def run(argv, timeout):
        seen["argv"] = argv
        (d / "S-1.plan.json").write_text(json.dumps({"tasks": [1, 2, 3], "planned_by": "opus"}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    r = dw.run_command({"id": 1, "verb": "plan", "args": {"project": "p", "sprint": "s1", "story_id": "S-1"}}, cfg, run=run)
    assert r == {"tasks": 3, "planned_by": "opus"}
    assert seen["argv"] == ["dis-agent", "plan", "-p", "p", "-s", "s1", "--story", "S-1"]


def test_plan_story_missing_and_cap(cfg):
    seed(cfg, STORIES)
    a = {"project": "p", "sprint": "s1", "story_id": "S-9"}
    with pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "plan", "args": a}, cfg)
    assert e.value.code == "story_not_found"
    (cfg.data_dir / "desk_caps.json").write_text(json.dumps({"opus_calls_today": 5, "opus_cap_per_day": 5}))
    with pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "plan", "args": {**a, "story_id": "S-1"}}, cfg)
    assert e.value.code == "cap_reached"


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "a b", "x\n"])
def test_bad_args(cfg, bad):
    with pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "refresh_proposal", "args": {"project": bad, "sprint": "s1"}}, cfg)
    assert e.value.code == "bad_args"


def test_step_done_evidence_and_unknown(cfg):
    a = {"project": "p", "sprint": "s1", "story_id": "S-1", "step": "x", "evidence": "  "}
    with pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "step_done", "args": a}, cfg)
    assert e.value.code == "evidence_required"
    with pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "deploy", "args": {}}, cfg)
    assert e.value.code == "unknown_verb"
    got = {}
    dw.run_command({"id": 1, "verb": "step_done", "args": {**a, "evidence": "ok; rm -rf"}}, cfg,
                   run=lambda argv, t: got.setdefault("a", argv) and SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert got["a"][-2:] == ["--evidence", "ok; rm -rf"]


class FakeClient:
    def __init__(self, cmd=None, running=()):
        self.calls, self.cmd, self.running = [], cmd, list(running)

    def heartbeat(self, s): self.calls.append(("hb", s))
    def claim(self): self.calls.append(("claim",)); return self.cmd
    def done(self, i, r): self.calls.append(("done", i, r))
    def failed(self, i, e): self.calls.append(("failed", i, e))
    def list_running(self): return self.running


def test_tick_disabled(cfg):
    c = FakeClient()
    dw.tick(c, cfg)
    assert c.calls == [("hb", "disabled: D-006 not confirmed")]


def enable(cfg):
    p = cfg.repo_root / dw.DECISIONS_REL
    p.parent.mkdir(parents=True)
    p.write_text("## D-006\nStatus: accepted\n")


def test_tick_runs_and_reports(cfg):
    enable(cfg)
    c = FakeClient({"id": 7, "verb": "x", "args": {}})
    dw.tick(c, cfg, run=lambda cmd, cfg: {"ok": 1})
    assert c.calls[-1] == ("done", 7, {"ok": 1})
    c = FakeClient({"id": 8, "verb": "x", "args": {}})
    dw.tick(c, cfg, run=lambda cmd, cfg: (_ for _ in ()).throw(dw.CommandError("cap_reached", "m")))
    assert c.calls[-1] == ("failed", 8, "cap_reached: m")


def test_tick_lock_held_skips_claim(cfg):
    enable(cfg)
    c = FakeClient({"id": 1})
    with dw.worker_lock(cfg.data_dir):
        dw.tick(c, cfg)
    assert ("claim",) not in c.calls


def test_on_start_and_backoff(cfg):
    c = FakeClient(running=[3, 4])
    dw.on_start(c, cfg)
    assert c.calls == [("failed", 3, "worker restarted"), ("failed", 4, "worker restarted")]
    b, seq = 0.0, []
    for _ in range(6):
        b = dw.next_backoff(b); seq.append(b)
    assert seq == [30, 60, 120, 240, 300, 300]


def test_client_network_error():
    def opener(*a, **k): raise dw.urllib.error.URLError("x")
    with pytest.raises(dw.OrchestratorUnreachable):
        dw.OrchestratorClient("http://o.invalid", "s", opener=opener).heartbeat("idle")
