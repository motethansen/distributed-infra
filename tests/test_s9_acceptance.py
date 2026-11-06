"""S-9 acceptance (independent): desk worker gate, proposal, plan/validate/step_done argv, caps, lock, restart, routing."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import desk_worker as dw  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROJ, SPR = "demo", "2"


def _cfg(tmp_path):
    return dw.Config("http://o", "s", tmp_path, tmp_path / "data")


def _sdir(tmp_path):
    d = tmp_path / "data" / "scrum" / PROJ / SPR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stories(tmp_path, stories=None):
    stories = stories or [{"id": "S-1", "title": "one", "points": 3},
                          {"id": "S-2", "title": "two", "points": 5},
                          {"id": "S-3", "title": "human", "points": 2, "human_only": True},
                          {"id": "S-4", "title": "four", "points": 1}]
    (_sdir(tmp_path) / "stories.json").write_text(json.dumps({"stories": stories}))


def _cmd(verb, story="S-2", **kw):
    return {"id": 7, "verb": verb, "project": PROJ, "sprint": SPR, "story_id": story, **kw}


class FakeClient:
    def __init__(self, queued=(), running=(), opus=None):
        self.queued, self.running, self.opus = list(queued), list(running), opus
        self.claims, self.results, self.heartbeats = [], [], []

    def list_commands(self, status):
        return list(self.queued if status == "queued" else self.running)

    def claim(self, i):
        self.claims.append(i); return True

    def done(self, i, r): self.results.append((i, "done", r))
    def failed(self, i, e): self.results.append((i, "failed", e))
    def opus_usage(self): return self.opus
    def heartbeat(self, state): self.heartbeats.append(state)


def _decisions(tmp_path, status):
    p = tmp_path / "decisions.md"
    p.write_text(f"# Decisions\n\n## D-005 — other\n\n**Status**: Decided\n\n## D-006 — Opus caps\n\n{status}\n\n## D-007 — x\n")
    return p


# 1
def test_c1_d006_not_accepted_claims_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("VIZNEO_REPO", str(tmp_path / "viz"))
    d = tmp_path / "viz" / "vantage"; d.mkdir(parents=True)
    (d / "decisions.md").write_text("## D-006 — Opus caps\n\n**Status**: Proposed\n")
    c = FakeClient(queued=[_cmd("refresh_proposal")])
    ran = []
    dw.tick(c, _cfg(tmp_path), run=lambda *a, **k: ran.append(1) or {})
    assert c.claims == [] and ran == [] and c.results == []


def test_c1_d006_status_detection(tmp_path):
    assert not dw.d006_accepted(_decisions(tmp_path, "**Status**: Proposed"))
    assert dw.d006_accepted(_decisions(tmp_path, "**Status**: **Decided by the owner on 2026-07-01**"))
    assert not dw.d006_accepted(tmp_path / "missing.md")


def test_c1_poll_interval_30s_and_launchd_job():
    assert dw.Config.from_env({}).poll_seconds == 30
    plist = (ROOT / "scripts" / "com.techstartups.desk-worker.plist.example").read_text()
    assert "<key>KeepAlive</key>" in plist and "desk_worker" in plist


def test_c1_heartbeat_state_disabled_d006_not_confirmed():
    # the criterion names this exact heartbeat state; it must exist somewhere in the worker
    assert "disabled: D-006 not confirmed" in (ROOT / "scripts" / "desk_worker.py").read_text()


# 2
def test_c2_refresh_proposal_shape_and_excludes_human_only(tmp_path):
    _stories(tmp_path)
    (_sdir(tmp_path) / "S-1.plan.json").write_text(json.dumps({"story": {"id": "S-1"}, "tasks": []}))
    r = dw.run_command(_cmd("refresh_proposal", story=None), _cfg(tmp_path), run=lambda *a, **k: pytest.fail("subprocess"))
    assert r["project"] == PROJ and r["sprint"] == SPR and r["generated_at"]
    assert r["planned"] == ["S-1"]
    assert [u["id"] for u in r["unplanned"]] == ["S-2", "S-4"]
    assert set(r["unplanned"][0]) == {"id", "title", "points"}
    assert "S-3" not in json.dumps(r["unplanned"])


# 3
def test_c3_plan_exists_fails_and_file_untouched(tmp_path):
    _stories(tmp_path)
    f = _sdir(tmp_path) / "S-2.plan.json"
    f.write_text('{"story":{"id":"S-2"},"tasks":[]}')
    os.utime(f, (1_000_000_000, 1_000_000_000))
    before = (f.read_bytes(), f.stat().st_mtime_ns)
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(_cmd("plan"), _cfg(tmp_path), run=lambda *a, **k: pytest.fail("ran"))
    assert e.value.code == "plan_exists"
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before


def test_c3_plan_runs_argv_list_no_shell_and_reports_counts(tmp_path):
    _stories(tmp_path)
    seen = {}

    def run(argv, timeout):
        seen["argv"] = argv
        (_sdir(tmp_path) / "S-2.plan.json").write_text(json.dumps(
            {"story": {"id": "S-2"}, "tasks": [{}, {}, {}], "planned_by": "scrum_master"}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    r = dw.run_command(_cmd("plan"), _cfg(tmp_path), run=run)
    assert isinstance(seen["argv"], list) and all(isinstance(a, str) for a in seen["argv"])
    assert "plan" in seen["argv"] and "S-2" in seen["argv"]
    assert r == {"tasks": 3, "planned_by": "scrum_master"}


def test_c3_default_runner_uses_shell_false(monkeypatch):
    calls = {}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.update(argv=argv, **kw) or SimpleNamespace(returncode=0))
    dw._run_argv(["x", "y"], 5)
    assert isinstance(calls["argv"], list) and calls["shell"] is False


# 4
def test_c4_validate_and_step_done_argv_lists(tmp_path):
    _stories(tmp_path)
    argvs = []

    def run(argv, timeout):
        argvs.append(argv); return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    dw.run_command(_cmd("validate"), _cfg(tmp_path), run=run)
    dw.run_command(_cmd("step_done", evidence="tests green; $(rm -rf /)"), _cfg(tmp_path), run=run)
    assert all(isinstance(a, list) for a in argvs) and len(argvs) == 2
    assert "validate" in argvs[0]
    assert any("tests green; $(rm -rf /)" in a for a in argvs[1])   # passed verbatim as one entry, never via a shell


@pytest.mark.parametrize("ev", [None, "", "   "])
def test_c4_step_done_requires_evidence(tmp_path, ev):
    _stories(tmp_path)
    with pytest.raises(dw.CommandError):
        dw.run_command(_cmd("step_done", evidence=ev), _cfg(tmp_path), run=lambda *a, **k: pytest.fail("ran"))


# 5
@pytest.mark.parametrize("opus", [{"spent": 50, "cap": 50, "day_spent": 0}, {"spent": 80, "cap": 50, "day_spent": 0}])
def test_c5_cap_reached_fails_no_model_call_no_retry(tmp_path, opus):
    _stories(tmp_path)
    c = FakeClient(queued=[_cmd("plan")], opus=opus)
    calls = []
    cfg = _cfg(tmp_path)
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(_cmd("plan"), cfg, run=lambda *a, **k: calls.append(a), caps_hook=lambda: dw.check_caps(c))
    assert e.value.code == "cap_reached" and calls == []
    # through the poll loop: failed once with cap_reached, no re-claim
    monkey_dec = _decisions(tmp_path, "**Status**: Decided")
    os.environ["VIZNEO_REPO"] = str(tmp_path / "v"); (tmp_path / "v" / "vantage").mkdir(parents=True)
    (tmp_path / "v" / "vantage" / "decisions.md").write_text(monkey_dec.read_text())
    try:
        dw.tick(c, cfg, run=dw.run_command)
    finally:
        os.environ.pop("VIZNEO_REPO")
    fails = [r for r in c.results if r[1] == "failed"]
    assert len(fails) == 1 and fails[0][2].startswith("cap_reached") and len(c.claims) == 1


def test_c5_under_cap_allows():
    dw.check_caps(FakeClient(opus={"spent": 1, "cap": 50, "day_spent": 1}))


# 6
def test_c6_lock_is_exclusive_under_data_dir(tmp_path):
    d = tmp_path / "data"
    with dw.worker_lock(d):
        assert any(p.suffix == ".lock" or "lock" in p.name for p in d.iterdir())
        with pytest.raises(dw.LockBusy):
            with dw.worker_lock(d):
                pass
    with dw.worker_lock(d):
        pass


def test_c6_command_fails_busy_when_lock_held(tmp_path):
    _stories(tmp_path)
    with dw.worker_lock(tmp_path / "data"):
        with pytest.raises(dw.LockBusy):
            dw.run_command(_cmd("validate"), _cfg(tmp_path), run=lambda *a, **k: pytest.fail("ran"))


def test_c6_restart_marks_running_rows_failed(tmp_path):
    c = FakeClient(running=[{"id": 11}, {"id": 12}])
    dw.on_start(c, _cfg(tmp_path))
    assert c.results == [(11, "failed", "worker restarted"), (12, "failed", "worker restarted")]


def test_c6_one_command_per_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("VIZNEO_REPO", str(tmp_path / "v")); (tmp_path / "v" / "vantage").mkdir(parents=True)
    (tmp_path / "v" / "vantage" / "decisions.md").write_text("## D-006 — x\n\n**Status**: Decided\n")
    c = FakeClient(queued=[{**_cmd("validate"), "id": 1}, {**_cmd("validate"), "id": 2}])
    dw.tick(c, _cfg(tmp_path), run=lambda cmd, cfg: {"ok": True})
    assert c.claims == [1]


# 7
def test_c7_worker_makes_no_model_call_and_only_t1_verbs():
    from shared.desk_allowlist import ALLOWLIST
    assert set(dw.VERBS) == {"refresh_proposal", "plan", "validate", "step_done"}
    assert all(ALLOWLIST[v]["tier"] == "T1" for v in dw.VERBS)
    src = (ROOT / "scripts" / "desk_worker.py").read_text()
    assert not re.search(r"^\s*(import|from)\s+(anthropic|openai|agents|ollama)", src, re.M)


def test_c7_plan_role_is_not_deepseek_for_private_prompt():
    from agents import roles
    r = roles.resolve("scrum_master", project="vantage", sensitivity="private", prompt="holdings: AAPL 100 shares")
    assert r["agent"] != "deepseek"
