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


# ---- S-9 spec tests: subprocess-level behaviour, tick flow, lock, restart ----
import fcntl
from unittest import mock

PLAN_ARGS = {"project": "p", "sprint": "s1", "story_id": "S-1"}


def ok_proc(**kw):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_tick_claims_only_when_d006_accepted(cfg):
    c = FakeClient()
    dw.tick(c, cfg)
    assert [x for x in c.calls if x[0] == "claim"] == [] and c.calls == [("hb", "disabled: D-006 not confirmed")]
    enable(cfg)
    c = FakeClient()
    dw.tick(c, cfg)
    assert [x for x in c.calls if x[0] == "claim"] == [("claim",)]


def test_refresh_proposal_shape_no_subprocess(cfg):
    seed(cfg, STORIES, plans=["S-2"])
    with mock.patch.object(dw.subprocess, "run") as sub:
        r = dw.run_command({"id": 1, "verb": "refresh_proposal", "args": {"project": "p", "sprint": "s1"}}, cfg)
    assert r["unplanned"] == [{"id": "S-1", "title": "a", "points": 2}]
    assert r["planned"] == ["S-2"] and "S-3" not in json.dumps(r) and r["generated_at"]
    sub.assert_not_called()


def test_plan_exists_bytes_and_mtime_unchanged(cfg):
    d = seed(cfg, STORIES, plans=["S-1"])
    f = d / "S-1.plan.json"
    before, mt = f.read_bytes(), f.stat().st_mtime_ns
    with mock.patch.object(dw.subprocess, "run") as sub, pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "plan", "args": PLAN_ARGS}, cfg)
    assert e.value.code == "plan_exists"
    assert f.read_bytes() == before and f.stat().st_mtime_ns == mt
    sub.assert_not_called()


def test_double_select_second_plan_fails(cfg):
    d = seed(cfg, STORIES)

    def run(argv, timeout):
        (d / "S-1.plan.json").write_text(json.dumps({"tasks": [1], "planned_by": "opus"}))
        return ok_proc()
    cmd = {"id": 1, "verb": "plan", "args": PLAN_ARGS}
    assert dw.run_command(cmd, cfg, run=run)["tasks"] == 1
    with pytest.raises(dw.CommandError) as e:
        dw.run_command({**cmd, "id": 2}, cfg, run=run)
    assert e.value.code == "plan_exists"


def test_plan_subprocess_argv_not_shell(cfg):
    d = seed(cfg, STORIES)

    def fake(argv, **kw):
        (d / "S-1.plan.json").write_text(json.dumps({"tasks": [1, 2], "planned_by": "opus"}))
        return ok_proc()
    with mock.patch.object(dw.subprocess, "run", side_effect=fake) as sub:
        r = dw.run_command({"id": 1, "verb": "plan", "args": PLAN_ARGS}, cfg)
    assert sub.call_args.args[0] == ["dis-agent", "plan", "-p", "p", "-s", "s1", "--story", "S-1"]
    assert sub.call_args.kwargs.get("shell") is not True
    assert r == {"tasks": 2, "planned_by": "opus"}


def test_story_not_found_message(cfg):
    seed(cfg, STORIES)
    with pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "plan", "args": {**PLAN_ARGS, "story_id": "S-42"}}, cfg)
    assert e.value.code == "story_not_found" and e.value.message == "story S-42 not found"


def test_validate_and_step_done_argv_without_shell(cfg):
    seed(cfg, STORIES)
    with mock.patch.object(dw.subprocess, "run", return_value=ok_proc()) as sub:
        dw.run_command({"id": 1, "verb": "validate", "args": PLAN_ARGS}, cfg)
        dw.run_command({"id": 2, "verb": "step_done", "args": {**PLAN_ARGS, "step": "st1", "evidence": "tests green"}}, cfg)
    v, s = sub.call_args_list
    assert v.args[0] == ["dis-agent", "validate", "-p", "p", "-s", "s1", "--story", "S-1"]
    assert s.args[0] == ["dis-agent", "step-done", "-p", "p", "-s", "s1", "--story", "S-1",
                         "--step", "st1", "--evidence", "tests green"]
    assert all(c.kwargs.get("shell") is not True for c in (v, s))


@pytest.mark.parametrize("ev", [None, "", "   ", "\n\t"])
def test_step_done_requires_evidence(cfg, ev):
    with mock.patch.object(dw.subprocess, "run") as sub, pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "step_done", "args": {**PLAN_ARGS, "step": "st1", "evidence": ev}}, cfg)
    assert e.value.code == "evidence_required"
    sub.assert_not_called()


def test_cap_reached_fails_once_no_retry(cfg):
    enable(cfg)
    seed(cfg, STORIES)
    (cfg.data_dir / "desk_caps.json").write_text(json.dumps({"opus_calls_today": 3, "opus_cap_per_day": 3}))
    c = FakeClient({"id": 9, "verb": "plan", "args": PLAN_ARGS})
    with mock.patch.object(dw.subprocess, "run") as sub:
        dw.tick(c, cfg)
    sub.assert_not_called()
    failed = [x for x in c.calls if x[0] == "failed"]
    assert len(failed) == 1 and failed[0][1] == 9 and failed[0][2].startswith("cap_reached")
    assert [x for x in c.calls if x[0] == "claim"] == [("claim",)]
    assert not [x for x in c.calls if x[0] == "done"]


def test_restart_marks_running_failed():
    c = FakeClient(running=["c1"])
    dw.on_start(c, None)
    assert c.calls == [("failed", "c1", "worker restarted")]


def test_crash_inside_command_posts_nothing(cfg):
    enable(cfg)
    c = FakeClient({"id": 5, "verb": "x", "args": {}})

    class Crash(BaseException):
        pass

    def boom(cmd, cfg):
        raise Crash()
    with pytest.raises(Crash):
        dw.tick(c, cfg, run=boom)
    assert not [x for x in c.calls if x[0] in ("done", "failed")]


def test_lock_held_by_other_fd_blocks_claim(cfg):
    enable(cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    c = FakeClient({"id": 1, "verb": "x", "args": {}})
    with open(cfg.data_dir / "desk_worker.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        dw.tick(c, cfg, run=lambda *a: pytest.fail("must not run"))
    assert not [x for x in c.calls if x[0] == "claim"]


def test_orchestrator_unreachable_propagates_and_runs_nothing(cfg):
    enable(cfg)

    class Down(FakeClient):
        def claim(self):
            raise dw.OrchestratorUnreachable("URLError")
    c = Down()
    with mock.patch.object(dw.subprocess, "run") as sub, pytest.raises(dw.OrchestratorUnreachable):
        dw.tick(c, cfg)  # by design tick propagates; main() backs off
    sub.assert_not_called()
    assert not [x for x in c.calls if x[0] in ("done", "failed")]
