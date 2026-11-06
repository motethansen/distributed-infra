"""S-9 desk worker: no network, no model, no real subprocess."""
import fcntl
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("desk_worker", ROOT / "scripts" / "desk_worker.py")
dw = importlib.util.module_from_spec(_spec)
sys.modules["desk_worker"] = dw
_spec.loader.exec_module(dw)
from shared.desk_allowlist import ALLOWLIST  # noqa: E402

STORIES = {"stories": [{"id": "S-1", "title": "a", "points": 2}, {"id": "S-2", "title": "b", "points": 1},
                       {"id": "S-3", "title": "h", "points": 1, "human_only": True}]}


@pytest.fixture
def cfg(tmp_path):
    return dw.Config("http://orch.invalid", "s", tmp_path, tmp_path / "data")


def seed(cfg, stories=STORIES, plans=()):
    d = cfg.data_dir / "scrum" / "p" / "s1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "stories.json").write_text(json.dumps(stories))
    for sid in plans:
        (d / f"{sid}.plan.json").write_text(json.dumps({"story": {"id": sid}, "tasks": [1, 2], "planned_by": "claude"}))
    return d


def cmd(verb, story="S-1", **kw):
    return {"id": 7, "verb": verb, "project": "p", "sprint": "s1", "story_id": story, "tier": "T1", **kw}


class FakeClient:
    def __init__(self, queued=(), running=(), paused=False, claim_ok=True, opus=None):
        self.queued, self.running, self.paused, self.claim_ok, self.opus = list(queued), list(running), paused, claim_ok, opus
        self.calls = []

    def list_commands(self, status):
        return self.queued if status == "queued" else self.running

    def claim(self, cid):
        self.calls.append(("claim", cid))
        if self.paused:
            raise dw.ApiError(423)
        return self.claim_ok

    def done(self, cid, result):
        self.calls.append(("done", cid, result))

    def failed(self, cid, err):
        self.calls.append(("failed", cid, err))

    def opus_usage(self):
        return self.opus

    def heartbeat(self, state):
        self.calls.append(("heartbeat", state))


@pytest.fixture
def accepted(monkeypatch):
    monkeypatch.setattr(dw, "d006_accepted", lambda *a, **k: True)


# --- decisions.md -----------------------------------------------------------

def test_d006_real_format(tmp_path):
    f = tmp_path / "d.md"
    f.write_text("## D-005 — x\n\n**Status**: **Decided** 1\n\n## D-006 — Commands\n\n**Date**: 2026-11-04\n"
                 "**Status**: **Decided by the owner on 2026-11-06** with answers\n\n## D-007 — y\n")
    assert dw.d006_accepted(f)
    f.write_text("## D-005 — x\n**Status**: accepted\n## D-006 — Commands\n**Status**: Proposed\n## D-007\n**Status**: Accepted\n")
    assert not dw.d006_accepted(f)
    f.write_text("## D-006 — c\n**Status**: Accepted\n")
    assert dw.d006_accepted(f)
    f.write_text("## D-005\n**Status**: Decided\n")
    assert not dw.d006_accepted(f) and not dw.d006_accepted(tmp_path / "missing.md")


def test_d006_real_file_if_present():
    p = dw.viz_decisions_path()
    if p.exists():
        assert dw.d006_accepted(p)


# --- verbs and arguments ------------------------------------------------------

def test_worker_verbs_are_exactly_the_t1_allowlist():
    assert set(dw.VERBS) == {v for v, e in ALLOWLIST.items() if e["tier"] == "T1"}


@pytest.mark.parametrize("verb", ["approve", "go", "merge", "deploy", "close", "cancel", "accept", "po_request", "rm -rf", None])
def test_non_t1_verbs_refused(cfg, verb):
    with pytest.raises(dw.CommandError):
        dw.run_command(cmd(verb), cfg, run=lambda *a: pytest.fail("must not run"))


@pytest.mark.parametrize("bad", ["../x", "a b", "S-1; ls", "", None, "a\n", "x" * 65, ".."])
def test_bad_identifiers(cfg, bad):
    for key in ("project", "sprint", "story_id"):
        with pytest.raises(dw.CommandError) as e:
            dw.run_command({**cmd("validate"), key: bad}, cfg, run=lambda *a: pytest.fail("must not run"))
        assert e.value.code == "bad_args"


# --- refresh_proposal ---------------------------------------------------------

def test_refresh_proposal_matches_local_proposals(cfg):
    seed(cfg, plans=["S-2"])
    r = dw.run_command(cmd("refresh_proposal", story=None), cfg, run=lambda *a: pytest.fail("no subprocess"))
    assert r["todo"] == ["S-1"] and r["planned"] == ["S-2"]        # S-3 is human-only: excluded from both
    assert r["unplanned"] == [{"id": "S-1", "title": "a", "points": 2}]
    assert r["stories"] == 3 and r["points"] == 4 and r["dismissed"] is False


def test_refresh_proposal_dismissed_and_blank_plan(cfg):
    d = seed(cfg)
    (d / ".dismissed").write_text("x")
    (d / "S-1.plan.json").write_text("   ")
    r = dw.proposal_from_dir(cfg.data_dir, "p", "s1")
    # a dismissed sprint publishes no unplanned stories, as dis_agent._local_proposals skips it entirely
    assert r["dismissed"] and r["todo"] == [] and r["unplanned"] == [] and r["planned"] == []


def test_proposal_agrees_with_dis_agent(cfg, monkeypatch):
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "clients" / "fleet-mcp")]
    import dis_agent as da
    seed(cfg, {"project": "p", "sprint": "s1", **STORIES}, plans=["S-2"])
    monkeypatch.setattr(da, "ROOT", cfg.repo_root)
    theirs = da._local_proposals()["p"]
    mine = dw.proposal_from_dir(cfg.data_dir, "p", "s1")
    assert theirs["todo"] == mine["todo"] and theirs["planned"] == len(mine["planned"]) and theirs["points"] == mine["points"]


# --- plan ---------------------------------------------------------------------

def test_plan_argv_is_a_list_with_real_flags(cfg):
    d = seed(cfg)
    seen = {}

    def run(argv, timeout):
        seen["argv"], seen["timeout"] = argv, timeout
        (d / "S-1.plan.json").write_text(json.dumps({"tasks": [1, 2, 3], "planned_by": "claude"}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    caps = []
    r = dw.run_command(cmd("plan"), cfg, run=run, caps_hook=lambda: caps.append(1))
    assert r == {"tasks": 3, "planned_by": "claude"} and caps == [1]
    assert isinstance(seen["argv"], list)
    assert seen["argv"][1:] == [str(cfg.repo_root / "scripts" / "dis_agent.py"), "plan", "-p", "p", "-s", "s1", "--story", "S-1"]


def test_plan_exists_untouched(cfg):
    d = seed(cfg, plans=["S-1"])
    before = ((d / "S-1.plan.json").read_bytes(), (d / "S-1.plan.json").stat().st_mtime_ns)
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(cmd("plan"), cfg, run=lambda *a: pytest.fail("must not run"))
    assert e.value.code == "plan_exists"
    assert before == ((d / "S-1.plan.json").read_bytes(), (d / "S-1.plan.json").stat().st_mtime_ns)


def test_double_select_second_fails(cfg):
    d = seed(cfg)

    def run(argv, timeout):
        (d / "S-1.plan.json").write_text(json.dumps({"tasks": [], "planned_by": "x"}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    dw.run_command(cmd("plan"), cfg, run=run)
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(cmd("plan"), cfg, run=run)
    assert e.value.code == "plan_exists"


def test_story_not_found_message_equals_cmd_plan(cfg):
    d = seed(cfg)
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(cmd("plan", story="S-9"), cfg)
    assert e.value.code == "story_not_found"
    assert e.value.message == f"story S-9 not in {d / 'stories.json'}"          # dis_agent.cmd_plan: f"story {a.story} not in {f}"
    assert "not in {f}" in (ROOT / "scripts" / "dis_agent.py").read_text()


def test_plan_of_human_step_refused(cfg):
    seed(cfg)
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(cmd("plan", story="S-3"), cfg, run=lambda *a: pytest.fail("must not run"))
    assert e.value.code == "human_step"


def test_plan_failure_and_missing_file(cfg):
    seed(cfg)
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(cmd("plan"), cfg, run=lambda a, t: SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    assert e.value.code == "plan_failed" and "boom" in e.value.message
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(cmd("plan"), cfg, run=lambda a, t: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert e.value.code == "plan_failed"


# --- caps ---------------------------------------------------------------------

def test_cap_reached_blocks_plan_before_running(cfg):
    seed(cfg)
    client = FakeClient(opus={"spent": 50.0, "cap": 50.0, "day_spent": 0})
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(cmd("plan"), cfg, run=lambda *a: pytest.fail("must not run"), caps_hook=lambda: dw.check_caps(client))
    assert e.value.code == "cap_reached"


def test_caps_ok_below_cap():
    dw.check_caps(FakeClient(opus={"spent": 49.99, "cap": 50.0, "day_spent": 4.99}))


def test_daily_cap_reached():
    with pytest.raises(dw.CommandError) as e:
        dw.check_caps(FakeClient(opus={"spent": 10, "cap": 50.0, "day_spent": 5.0}))
    assert e.value.code == "cap_reached" and "daily" in e.value.message


def test_monthly_cap_uses_lower_of_config_and_reported(monkeypatch):
    monkeypatch.setattr(dw, "MONTHLY_CAP_USD", 20.0)
    with pytest.raises(dw.CommandError) as e:
        dw.check_caps(FakeClient(opus={"spent": 20, "cap": 50.0, "day_spent": 0}))
    assert e.value.code == "cap_reached" and "monthly" in e.value.message


@pytest.mark.parametrize("opus", [None, {}, {"spent": 1, "cap": 50}, {"spent": "x", "cap": 50, "day_spent": 0},
                                  {"spent": None, "cap": 50, "day_spent": 0}])
def test_caps_fail_closed_on_unknown_usage(opus):
    with pytest.raises(dw.CommandError) as e:
        dw.check_caps(FakeClient(opus=opus))
    assert e.value.code == "cap_unknown"


def test_caps_fail_closed_when_unverifiable():
    class Down(FakeClient):
        def opus_usage(self):
            raise dw.OrchestratorUnreachable("x")
    with pytest.raises(dw.CommandError) as e:
        dw.check_caps(Down())
    assert e.value.code == "cap_unknown"


@pytest.mark.parametrize("verb", ["plan", "validate"])
def test_model_calling_verbs_check_caps_before_running(cfg, verb):
    seed(cfg)
    client = FakeClient(opus=None)
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(cmd(verb), cfg, run=lambda *a: pytest.fail("must not run"), caps_hook=lambda: dw.check_caps(client))
    assert e.value.code == "cap_unknown"


def test_refresh_proposal_and_step_done_skip_caps(cfg):
    seed(cfg)
    hook = lambda: pytest.fail("no model call, no cap check")  # noqa: E731
    dw.run_command(cmd("refresh_proposal"), cfg, caps_hook=hook)
    dw.run_command(cmd("step_done", evidence="e"), cfg, run=lambda a, t: SimpleNamespace(returncode=0, stdout="", stderr=""), caps_hook=hook)


@pytest.mark.parametrize("verb", ["plan", "validate"])
def test_under_cap_runs(cfg, verb):
    d = seed(cfg)
    ran = []

    def run(argv, timeout):
        ran.append(argv)
        (d / "S-1.plan.json").write_text(json.dumps({"story": {"id": "S-1"}, "tasks": [1]}))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")
    c = FakeClient(opus={"spent": 1, "cap": 50, "day_spent": 1})
    dw.run_command(cmd(verb), cfg, run=run, caps_hook=lambda: dw.check_caps(c))
    assert len(ran) == 1


def test_cap_reached_fails_once_no_retry(cfg, accepted):
    seed(cfg)
    c = FakeClient(queued=[{"id": 7, **cmd("plan")}], opus={"spent": 60, "cap": 50, "day_spent": 0})
    dw.tick(c, cfg)
    assert [x[0] for x in c.calls] == ["claim", "failed"] and c.calls[1][2].startswith("cap_reached")


# --- validate / step_done -------------------------------------------------------

def test_validate_argv(cfg):
    seed(cfg)
    seen = []
    r = dw.run_command(cmd("validate"), cfg, run=lambda a, t: seen.append(a) or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    assert r == {"ok": True, "output": "ok"} and seen[0][2:] == ["validate", "-p", "p", "-s", "s1", "--story", "S-1"]


def test_step_done_uses_dis_agent_done(cfg):
    seed(cfg)
    seen = []
    dw.run_command(cmd("step_done", story="S-3", evidence="--yes; rm -rf /"), cfg,
                   run=lambda a, t: seen.append(a) or SimpleNamespace(returncode=0, stdout="done", stderr=""))
    assert seen[0][2:] == ["done", "-p", "p", "-s", "s1", "--story", "S-3", "--evidence=--yes; rm -rf /", "--yes"]


@pytest.mark.parametrize("ev", [None, "", "   "])
def test_step_done_requires_evidence(cfg, ev):
    with pytest.raises(dw.CommandError) as e:
        dw.run_command(cmd("step_done", story="S-3", evidence=ev), cfg, run=lambda *a: pytest.fail("must not run"))
    assert e.value.code == "evidence_required"


# --- tick ---------------------------------------------------------------------

def test_tick_gate_and_no_claim_when_d006_unconfirmed(cfg, monkeypatch):
    monkeypatch.setattr(dw, "d006_accepted", lambda *a, **k: False)
    c = FakeClient(queued=[cmd("validate")])
    dw.tick(c, cfg)
    assert c.calls == [("heartbeat", "disabled: D-006 not confirmed")]


def test_tick_runs_and_reports(cfg, accepted):
    c = FakeClient(queued=[cmd("validate")])
    dw.tick(c, cfg, run=lambda cm, cf: {"ok": True})
    assert c.calls == [("claim", 7), ("done", 7, {"ok": True})]


def test_tick_skips_non_t1_rows_and_claims_one_per_tick(cfg, accepted):
    c = FakeClient(queued=[{"id": 1, "verb": "approve"}, {"id": 2, "verb": "validate"}, {"id": 3, "verb": "validate"}])
    dw.tick(c, cfg, run=lambda cm, cf: {})
    assert [x[:2] for x in c.calls] == [("claim", 2), ("done", 2)]


def test_tick_lost_claim_race_tries_next(cfg, accepted):
    c = FakeClient(queued=[cmd("validate")], claim_ok=False)
    dw.tick(c, cfg, run=lambda cm, cf: pytest.fail("not claimed"))
    assert c.calls == [("claim", 7)]


def test_tick_paused_leaves_rows_queued(cfg, accepted):
    c = FakeClient(queued=[cmd("validate"), {**cmd("validate"), "id": 8}], paused=True)
    dw.tick(c, cfg, run=lambda cm, cf: pytest.fail("paused"))
    assert c.calls == [("claim", 7)]


def test_tick_lock_held_by_other_fd_skips(cfg, accepted):
    cfg.data_dir.mkdir(parents=True)
    with open(cfg.data_dir / "desk_worker.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        c = FakeClient(queued=[cmd("validate")])
        dw.tick(c, cfg)
    assert c.calls == []


def test_unexpected_error_reports_type_only(cfg, accepted):
    def boom(cm, cf):
        raise ValueError("secret untrusted text")
    c = FakeClient(queued=[cmd("validate")])
    dw.tick(c, cfg, run=boom)
    assert c.calls[1] == ("failed", 7, "internal_error: ValueError")


def test_crash_inside_command_posts_nothing(cfg, accepted):
    class Crash(BaseException):
        pass

    def boom(cm, cf):
        raise Crash()
    c = FakeClient(queued=[cmd("validate")])
    with pytest.raises(Crash):
        dw.tick(c, cfg, run=boom)
    assert [x[0] for x in c.calls] == ["claim"]     # stays 'running'; on_start fails it after a restart


def test_orchestrator_unreachable_propagates(cfg, accepted):
    class Down(FakeClient):
        def list_commands(self, status):
            raise dw.OrchestratorUnreachable("x")
    with pytest.raises(dw.OrchestratorUnreachable):
        dw.tick(Down(), cfg, run=lambda *a: pytest.fail("nothing runs"))


def test_on_start_fails_leftover_running_rows(cfg):
    c = FakeClient(running=[{"id": 4}, {"id": 5}])
    dw.on_start(c, cfg)
    assert c.calls == [("failed", 4, "worker restarted"), ("failed", 5, "worker restarted")]


def test_on_start_refuses_and_touches_nothing_while_lock_held(cfg):
    c = FakeClient(running=[{"id": 4}])
    with dw.worker_lock(cfg.data_dir):
        with pytest.raises(dw.LockBusy):
            dw.on_start(c, cfg)
    assert c.calls == []


def test_on_start_pages_through_all_running_rows(cfg):
    class Paged(FakeClient):   # like the endpoint: oldest 50 'running' rows per call; failing a row removes it
        def list_commands(self, status):
            return [{"id": i} for i in self.running[:50]]

        def failed(self, cid, err):
            super().failed(cid, err)
            self.running.remove(cid)
    c = Paged(running=list(range(1, 131)))
    dw.on_start(c, cfg)
    assert [x[1] for x in c.calls] == list(range(1, 131))


def test_on_start_stops_when_rows_do_not_leave_running(cfg):
    class Stuck(FakeClient):
        def list_commands(self, status):
            return [{"id": 1}]
    c = Stuck()
    dw.on_start(c, cfg)
    assert c.calls == [("failed", 1, "worker restarted")]


def test_main_refuses_to_start_when_lock_held(cfg, monkeypatch, capsys):
    monkeypatch.setattr(dw.Config, "from_env", classmethod(lambda cls, env=None: cfg))
    monkeypatch.setattr(dw, "OrchestratorClient", lambda *a, **k: FakeClient(running=[{"id": 4}]))
    with dw.worker_lock(cfg.data_dir):
        with pytest.raises(SystemExit) as e:
            dw.main()
    assert e.value.code not in (0, None) and "another desk worker holds the lock" in str(e.value.code)


def test_disabled_d006_sets_heartbeat_state_and_claims_nothing(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("VIZNEO_REPO", str(tmp_path / "viz"))
    (tmp_path / "viz" / "vantage").mkdir(parents=True)
    (tmp_path / "viz" / "vantage" / "decisions.md").write_text("## D-006 — Opus caps\n\n**Status**: Proposed\n")
    c = FakeClient(queued=[cmd("validate")])
    dw.tick(c, cfg, run=lambda *a, **k: pytest.fail("ran"))
    assert c.calls == [("heartbeat", "disabled: D-006 not confirmed")]


def test_client_heartbeat_posts_state():
    seen = []

    def opener(req, timeout):
        seen.append((req.full_url, req.get_method(), json.loads(req.data)))
        return Resp(b"")
    dw.OrchestratorClient("http://o", "K", opener=opener).heartbeat("x")
    assert seen == [("http://o/workers/heartbeat", "POST", {"worker_name": "desk-worker", "state": "x"})]


def test_backoff():
    assert [dw.next_backoff(x) for x in (0, 30, 200, 300)] == [30, 60, 300, 300]


# --- HTTP client ------------------------------------------------------------------

class Resp:
    def __init__(self, body=b"{}"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def test_client_paths_headers_payloads():
    seen = []

    def opener(req, timeout):
        seen.append((req.method, req.full_url, req.headers, json.loads(req.data) if req.data else None))
        return Resp(b"[]" if "desk/commands" in req.full_url else b"{}")
    c = dw.OrchestratorClient("http://orch.invalid", "KEY", opener=opener)
    c.list_commands("queued"); c.claim(5); c.done(5, {"ok": True}); c.failed(5, "e")
    assert [(m, u.removeprefix("http://orch.invalid")) for m, u, _, _ in seen] == [
        ("GET", "/scrum/desk/commands?status=queued"), ("POST", "/scrum/responses/5/consume"),
        ("POST", "/scrum/responses/5/result"), ("POST", "/scrum/responses/5/result")]
    assert all(h["X-secret-key"] == "KEY" for _, _, h, _ in seen)
    assert seen[2][3] == {"status": "done", "summary": '{"ok":true}'} and seen[3][3] == {"status": "failed", "summary": "e"}


@pytest.mark.parametrize("code,result", [(409, False), (404, False)])
def test_claim_lost(code, result):
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
    assert dw.OrchestratorClient("http://o", "k", opener=opener).claim(1) is result


def test_claim_paused_raises_api_error_and_5xx_is_unreachable():
    def paused(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 423, "x", {}, None)

    def broken(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 502, "x", {}, None)
    with pytest.raises(dw.ApiError):
        dw.OrchestratorClient("http://o", "k", opener=paused).claim(1)
    with pytest.raises(dw.OrchestratorUnreachable):
        dw.OrchestratorClient("http://o", "k", opener=broken).claim(1)


def test_client_network_error_does_not_leak_url():
    def opener(req, timeout):
        raise urllib.error.URLError("http://secret.host refused")
    with pytest.raises(dw.OrchestratorUnreachable) as e:
        dw.OrchestratorClient("http://secret.host", "KEY", opener=opener).claim(1)
    assert "secret.host" not in str(e.value) and "KEY" not in str(e.value)


def test_summary_large_unplanned_list_stays_valid_json():
    unplanned = [{"id": f"S-{i}", "title": "t" * 40, "points": 1} for i in range(200)]
    r = {"project": "p", "sprint": "s1", "dismissed": False, "stories": 200, "points": 200, "todo": [u["id"] for u in unplanned],
         "planned": ["S-X"], "unplanned": unplanned, "generated_at": "now"}
    out = dw._summary(r)
    d = json.loads(out)
    assert len(out) <= 2000 and d["truncated"] is True and d["total"] == 200
    assert 0 < len(d["unplanned"]) < 200 and d["unplanned"] == unplanned[:len(d["unplanned"])]
    assert {"project", "sprint", "unplanned", "planned", "generated_at"} <= set(d)


def test_summary_small_result_untouched():
    r = {"project": "p", "unplanned": [], "planned": []}
    assert json.loads(dw._summary(r)) == r


def test_summary_is_capped():
    assert len(dw._summary({"ok": True, "output": "x" * 5000})) <= 2000


def test_new_worker_endpoint_lists_typed_fields():
    from shared import desk_store as ds
    assert "project" in ds.WORKER_COMMAND_COLS and "evidence" in ds.WORKER_COMMAND_COLS


@pytest.mark.parametrize("opus", [
    {"spent": float("nan"), "cap": 50.0, "day_spent": 0},
    {"spent": 1, "cap": 50.0, "day_spent": float("nan")},
    {"spent": 1, "cap": float("nan"), "day_spent": 0},
    {"spent": float("inf"), "cap": 50.0, "day_spent": 0},
    {"spent": -1, "cap": 50.0, "day_spent": 0},
    {"spent": 1, "cap": None, "day_spent": 0},            # a missing or null cap must not fall back to the default
    {"spent": 1, "day_spent": 0},
    {"spent": True, "cap": 50.0, "day_spent": 0},
    {"spent": "ten", "cap": 50.0, "day_spent": 0},
])
def test_non_finite_missing_or_garbled_figures_are_cap_unknown(opus):
    with pytest.raises(dw.CommandError) as ei:
        dw.check_caps(FakeClient(opus=opus))
    assert ei.value.code == "cap_unknown"


def test_a_cap_of_zero_means_nothing_may_be_spent():
    with pytest.raises(dw.CommandError) as ei:
        dw.check_caps(FakeClient(opus={"spent": 0, "cap": 0, "day_spent": 0}))
    assert ei.value.code == "cap_reached"


@pytest.mark.parametrize("bad", ["nan", "inf", "-3", "abc", "0"])
def test_bad_cap_environment_values_fall_back_to_the_d006_defaults(monkeypatch, bad):
    monkeypatch.setenv("DESK_DAILY_CAP_USD", bad)
    monkeypatch.setenv("DESK_MONTHLY_CAP_USD", bad)
    assert dw._cap_from_env("DESK_DAILY_CAP_USD", 5.0) == 5.0
    assert dw._cap_from_env("DESK_MONTHLY_CAP_USD", 50.0) == 50.0
