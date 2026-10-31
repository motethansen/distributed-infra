"""dis-agent CLI pieces that do not need a live fleet."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "clients" / "fleet-mcp")]
import dis_agent as da  # noqa: E402
import scrum_cli as sc  # noqa: E402


def test_opus_is_only_reachable_through_the_capped_product_owner_role():
    assert da.MODELS["opus"] == (None, "product_owner")
    assert all(a != "claude" or r is None for (a, r) in da.MODELS.values())          # no model name maps claude+opus directly
    assert da.MODELS["auto"] == (None, None) and da.MODELS["deepseek"] == ("deepseek", None)


def test_show_names_who_answered_and_who_was_skipped():
    out = da._show({"response": "42", "answered_by": "codex",
                    "failover": [{"agent": "claude", "reason": "limit", "until": "12:50"}]})
    assert "42" in out and "— codex" in out and "claude skipped (limit, back 12:50)" in out
    assert da._show({"error": "boom"}).startswith("! boom")


def test_every_role_and_machine_the_cli_offers_exists_in_the_shipped_config():
    cfg = da.projects_cfg()
    assert set(cfg["machines"]) == {"mac-mini", "thinkpad-x1"}                         # the MacBook is never assigned work
    for name, p in cfg["projects"].items():
        assert p["repo"].startswith(("git@", "https://")) and p["dir"], name
    assert "urbanlife" in cfg["projects"] and {"product_owner", "reviewer"} <= set(sc.role_hints())


def test_chat_help_mentions_every_command_the_loop_handles():
    for cmd in ("/model", "/role", "/machine", "/project", "/new", "/who", "/quit"):
        assert cmd in da.HELP


def test_wrapper_script_is_executable_and_points_at_the_python_entry():
    w = ROOT / "scripts" / "dis-agent"
    assert w.stat().st_mode & 0o111 and "scripts/dis_agent.py" in w.read_text()


def test_close_never_runs_without_an_interactive_yes_or_the_explicit_flag(tmp_path, monkeypatch, capsys):
    """A piped/non-interactive close must not touch the backlog unless --yes is passed."""
    import argparse
    import json
    plan_dir = tmp_path / "data" / "scrum" / "demo" / "S"
    plan_dir.mkdir(parents=True)
    (plan_dir / "S-1.plan.json").write_text(json.dumps({"story": {"id": "S-1", "title": "t"}, "backlog_id": "DM-002", "tasks": []}))
    monkeypatch.setattr(da, "ROOT", tmp_path)
    done = [{"id": "00000001x", "status": "done", "agent": "claude", "created_at": "2026-10-31T10:00:00",
             "payload": {"project": "demo", "sprint": "S", "story": "S-1", "role": "dev_frontend"}, "result": {"response": "pull/3"}},
            {"id": "00000002x", "status": "done", "agent": "codex", "created_at": "2026-10-31T10:05:00",
             "payload": {"project": "demo", "sprint": "S", "story": "S-1", "role": "reviewer"}, "result": {"response": "ok"}},
            {"id": "00000003x", "status": "done", "agent": "agy", "created_at": "2026-10-31T10:03:00",
             "payload": {"project": "demo", "sprint": "S", "story": "S-1", "role": "tester"},
             "result": {"response": '{"validation":{"criteria":[{"n":1,"status":"pass","evidence":"t"}]}}'}}]

    class C:
        def _call(self, *a, **k):
            return done
    monkeypatch.setattr(sc, "client", lambda: C())
    closed = []
    monkeypatch.setattr(da.sf, "close_story", lambda *a, **k: closed.append(a) or {"committed": True, "pushed": True, "push_error": None})
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    a = argparse.Namespace(project="demo", sprint="S", story=None, yes=False, force=False)
    da.cmd_close(a)
    assert closed == [] and "needs your confirmation" in capsys.readouterr().out
    a.yes = True
    da.cmd_close(a)
    assert len(closed) == 1 and closed[0][2] == "DM-002"


def test_close_refuses_unfinished_or_unreviewed_stories_even_with_yes(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    d = tmp_path / "data" / "scrum" / "demo" / "S"
    d.mkdir(parents=True)
    (d / "S-1.plan.json").write_text(json.dumps({"story": {"id": "S-1", "title": "t"}, "backlog_id": "DM-002", "tasks": []}))
    monkeypatch.setattr(da, "ROOT", tmp_path)
    rows = [{"id": "00000001x", "status": "done", "agent": "claude", "created_at": "x",
             "payload": {"project": "demo", "sprint": "S", "story": "S-1", "role": "dev_frontend"}, "result": {}}]
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: rows})())
    closed = []
    monkeypatch.setattr(da.sf, "close_story", lambda *a, **k: closed.append(a))
    da.cmd_close(argparse.Namespace(project="demo", sprint="S", story=None, yes=True, force=False))
    assert closed == [] and "no review ran" in capsys.readouterr().out


def test_close_refuses_when_a_task_is_unfinished_even_if_a_review_exists(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    d = tmp_path / "data" / "scrum" / "demo" / "S"
    d.mkdir(parents=True)
    (d / "S-1.plan.json").write_text(json.dumps({"story": {"id": "S-1", "title": "t"}, "backlog_id": "DM-002", "tasks": []}))
    monkeypatch.setattr(da, "ROOT", tmp_path)
    p = lambda role: {"project": "demo", "sprint": "S", "story": "S-1", "role": role}   # noqa: E731
    rows = [{"id": "00000001x", "status": "needs_human", "agent": "claude", "created_at": "1", "payload": p("dev_frontend"), "result": {}},
            {"id": "00000002x", "status": "done", "agent": "codex", "created_at": "2", "payload": p("reviewer"), "result": {"response": "ok"}}]
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: rows})())
    closed = []
    monkeypatch.setattr(da.sf, "close_story", lambda *a, **k: closed.append(a))
    da.cmd_close(argparse.Namespace(project="demo", sprint="S", story=None, yes=True, force=False))
    assert closed == [] and "not every task is done" in capsys.readouterr().out


def _close_with(tmp_path, monkeypatch, tester_response, capsys, check=False):
    import argparse
    import json
    d = tmp_path / "data" / "scrum" / "demo" / "S"
    d.mkdir(parents=True, exist_ok=True)
    (d / "S-1.plan.json").write_text(json.dumps({"story": {"id": "S-1", "title": "t"}, "backlog_id": "DM-002", "tasks": []}))
    monkeypatch.setattr(da, "ROOT", tmp_path)
    p = lambda role: {"project": "demo", "sprint": "S", "story": "S-1", "role": role}   # noqa: E731
    rows = [{"id": "00000001x", "status": "done", "agent": "claude", "created_at": "1", "payload": p("dev_frontend"), "result": {}},
            {"id": "00000002x", "status": "done", "agent": "codex", "created_at": "5", "payload": p("reviewer"), "result": {"response": "ok"}}]
    if tester_response is not None:
        rows.append({"id": "00000003x", "status": "done", "agent": "agy", "created_at": "3", "payload": p("tester"), "result": {"response": tester_response}})
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: rows})())
    closed = []
    monkeypatch.setattr(da.sf, "close_story", lambda *a, **k: closed.append(a) or {"committed": True, "pushed": True, "push_error": None})
    da.cmd_close(argparse.Namespace(project="demo", sprint="S", story=None, yes=not check, force=False, check=check))
    return closed, capsys.readouterr().out


def test_close_needs_a_passing_independent_validation_even_with_yes(tmp_path, monkeypatch, capsys):
    pass_ = '{"validation":{"criteria":[{"n":1,"status":"pass","evidence":"t1"}]}}'
    closed, out = _close_with(tmp_path, monkeypatch, pass_, capsys)
    assert len(closed) == 1 and "1 pass, 0 fail" in out
    for resp, why in ((None, "no validation ran"),
                      ('{"validation":{"criteria":[{"n":1,"status":"fail","evidence":"boom"}]}}', "1 fail"),
                      ('{"validation":{"criteria":[{"n":1,"status":"unverified","evidence":"needs live"}]}}', "1 unverified"),
                      ("garbage", "did not return a validation block")):
        closed, out = _close_with(tmp_path, monkeypatch, resp, capsys)
        assert closed == [] and "validation did not pass" in out and why in out, why


# ── PO revise / planning / groom ─────────────────────────────────────────────

def _ns(**kw):
    import argparse
    base = dict(project="demo", sprint="S", as_agent=None, approved=False, context_file=None, no_context=True, request=None, revise=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_po_revise_sends_the_stored_stories_and_keeps_the_earlier_round(tmp_path, monkeypatch, capsys):
    import json
    d = tmp_path / "data" / "scrum" / "demo" / "S"
    d.mkdir(parents=True)
    (d / "stories.json").write_text(json.dumps({"request": "gift money", "stories": [{"id": "S-1", "title": "old", "acceptance": ["a"]}]}))
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(sc, "client", lambda: object())
    seen = {}

    def fake(c, project, sprint, request, ctx, approved, as_agent, previous=None, feedback=""):
        seen.update(request=request, previous=previous, feedback=feedback)
        return [{"id": "S-1", "title": "new", "acceptance": ["a"]}], {"agent": "claude", "model": "opus", "concerns": ["hmm"]}
    monkeypatch.setattr(da.sf, "propose_stories", fake)
    da.cmd_po(_ns(revise="make it smaller"))
    assert seen == {"request": "gift money", "previous": [{"id": "S-1", "title": "old", "acceptance": ["a"]}], "feedback": "make it smaller"}
    assert json.loads((d / "stories.json").read_text())["stories"][0]["title"] == "new"
    assert json.loads((d / "stories.v1.json").read_text())["stories"][0]["title"] == "old"
    assert "concern: hmm" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        da.cmd_po(_ns(project="nosuch", revise="x"))
    with pytest.raises(SystemExit):
        da.cmd_po(_ns())                                          # neither a request nor feedback


def test_planning_loops_on_feedback_until_accepted_and_refuses_a_non_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(sc, "client", lambda: object())
    rounds = []

    def fake(c, project, sprint, request, ctx, approved, as_agent, previous=None, feedback=""):
        rounds.append((request, feedback))
        return [{"id": "S-1", "title": f"v{len(rounds)}", "acceptance": ["a"]}], {"agent": "claude", "model": "opus", "concerns": []}
    monkeypatch.setattr(da.sf, "propose_stories", fake)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    with pytest.raises(SystemExit, match="interactive"):
        da.cmd_planning(_ns())
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    answers = iter(["gift money", "split the UI out", "drop currency", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    da.cmd_planning(_ns())
    assert rounds == [("gift money", ""), ("gift money", "split the UI out"), ("gift money", "drop currency")]
    assert "Accepted after 2 revision(s)" in capsys.readouterr().out


def test_groom_writes_only_a_report(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path / "nosuch")
    monkeypatch.setattr(sc, "client", lambda: object())
    monkeypatch.setattr(da.sf, "groom_backlog", lambda *a, **k: ({"summary": "mixed", "review": [
        {"id": "DM-001", "verdict": "ok"}, {"id": "DM-002", "verdict": "stale", "issues": ["probably shipped"]}],
        "missing": [{"title": "rate limits", "why": "none exist"}]}, {"agent": "claude", "model": "opus"}))
    da.cmd_groom(_ns())
    out = capsys.readouterr().out
    assert "ok 1, stale 1" in out and "DM-002" in out and "probably shipped" in out and "rate limits" in out and "Nothing in the backlog was changed" in out
    assert list((tmp_path / "data" / "scrum" / "demo" / "grooming").glob("groom-*.json"))


# ── scrum of scrums / review queue ───────────────────────────────────────────

def _board_with(review=(), opus=None):
    return {"fleet": {"totals": {"tasks": 10, "done": 9, "tokens_out": 1234}},
            "digest": {"as_of": "2026-10-31", "review_queue": list(review), "decisions": [], "projects": [], "adjustments": [], "opus": opus}}


def test_scrum_prints_the_digest_saves_it_and_narrates_only_when_asked(tmp_path, monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(da, "ROOT", tmp_path)
    queued = []

    class C:
        def _call(self, *a, **k):
            return _board_with([{"project": "p", "id": "P-1", "title": "T", "evidence": "PR 9"}], {"spent": 4.0, "cap": 50.0})

        def queue(self, payload, notes):
            queued.append(payload)
            return {"id": "t1"}

        def wait(self, ids, **k):
            return {"t1": {"status": "done", "result": {"response": "BRIEFING TEXT"}}}
    monkeypatch.setattr(sc, "client", lambda: C())
    da.cmd_scrum(argparse.Namespace(narrate=False, as_agent=None))
    out = capsys.readouterr().out
    assert "Ready for your review (1)" in out and "P-1" in out and "Opus this month $4.00 of $50" in out and queued == []
    assert (tmp_path / "data" / "scrum" / "_all" / "2026-10-31.md").exists()
    da.cmd_scrum(argparse.Namespace(narrate=True, as_agent=None))
    assert queued[0]["role"] == "scrum_master" and "Do not invent facts" in queued[0]["prompt"] and "P-1" in queued[0]["prompt"]
    assert "BRIEFING TEXT" in capsys.readouterr().out


def test_review_lists_the_queue_with_sprint_prs_and_the_confirming_command(tmp_path, monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(da, "projects_cfg", lambda: {"projects": {"p": {"repo": "git@github.com:o/p.git"}}})
    board = _board_with([{"project": "p", "id": "P-1", "title": "A thing", "evidence": "PR #9 green, waiting for you to merge"},
                         {"project": "p", "id": "P-2", "title": "No PR", "evidence": "verified by hand"}])
    board["projects"] = [{"project": "p", "backlog_view": {"sprints": [{"sprint": "3", "stories": [{"id": "P-1"}]}], "unscheduled": [], "cancelled": []}}]
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: board})())
    da.cmd_review(argparse.Namespace(project=None, sprint=None))
    out = capsys.readouterr().out
    assert "2 stories wait for your sprint review" in out and "P-1  A thing   [sprint 3]" in out and "P-2  No PR   [not in a sprint]" in out
    assert "PRs:      https://github.com/o/p/pull/9" in out
    assert "confirm:  dis-agent accept -p p --story P-1 --pr https://github.com/o/p/pull/9" in out
    assert 'confirm:  dis-agent accept -p p --story P-2 --evidence "what you verified"' in out
    da.cmd_review(argparse.Namespace(project="p", sprint=None))                              # -p alone is the queue for that project
    assert "2 stories wait" in capsys.readouterr().out
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: _board_with()})())
    da.cmd_review(argparse.Namespace(project=None, sprint=None))
    assert "Nothing is waiting" in capsys.readouterr().out


def test_accept_needs_evidence_checks_github_when_given_a_pr_and_only_closes_on_a_yes(tmp_path, monkeypatch, capsys):
    import argparse
    closed = []
    ok = {"committed": True, "pushed": True, "push_error": None}
    monkeypatch.setattr(da.sf, "close_story", lambda *a, **k: closed.append(a) or ok)
    monkeypatch.setattr(da.sf, "find_sprint_of", lambda *a: None)
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    ns = lambda **k: argparse.Namespace(**{"project": "p", "story": "P-1", "pr": None, "evidence": None, "yes": False, **k})   # noqa: E731
    with pytest.raises(SystemExit, match="needs evidence"):
        da.cmd_accept(ns(yes=True))
    with pytest.raises(SystemExit, match="needs evidence"):
        da.cmd_accept(ns(evidence="   ", yes=True))
    with pytest.raises(SystemExit, match="needs your confirmation"):
        da.cmd_accept(ns(evidence="checked the response"))
    assert closed == []
    da.cmd_accept(ns(evidence="checked the response", yes=True))
    assert closed[0][2] == "P-1" and closed[0][5] == "checked the response"
    monkeypatch.setattr(da.sf, "verify_pr_merged", lambda url: {"merged_at": "2026-10-30", "title": "Ledger"})
    da.cmd_accept(ns(pr="https://github.com/o/p/pull/9", evidence="CI green", yes=True))
    assert closed[1][5] == "https://github.com/o/p/pull/9 merged 2026-10-30; CI green" and "GitHub confirms" in capsys.readouterr().out

    def unmerged(url):
        raise da.sf.PlanError(f"{url} is OPEN, not merged")
    monkeypatch.setattr(da.sf, "verify_pr_merged", unmerged)
    with pytest.raises(SystemExit, match="not accepted: .*is OPEN, not merged"):
        da.cmd_accept(ns(pr="https://github.com/o/p/pull/9", yes=True))
    assert len(closed) == 2                                                                 # an unmerged PR never closes anything
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda p="": "n")
    da.cmd_accept(ns(evidence="x"))
    assert len(closed) == 2 and "left in review" in capsys.readouterr().out


def test_accept_also_updates_a_dis_agent_sprint_file_when_the_story_has_one(tmp_path, monkeypatch):
    import argparse
    marks = []
    ok = {"committed": True, "pushed": True, "push_error": None}
    monkeypatch.setattr(da.sf, "close_story", lambda *a, **k: ok)
    monkeypatch.setattr(da.sf, "find_sprint_of", lambda *a: 4)
    monkeypatch.setattr(da.sf, "sprint_mark", lambda *a, **k: marks.append((a[2:5], k)) or ok)
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path)
    da.cmd_accept(argparse.Namespace(project="p", story="P-1", pr=None, evidence="verified", yes=True))
    (args, kw), = marks
    assert args[0] == 4 and args[1] == "P-1" and args[2].startswith("✅ done ") and kw["closing"] is True


def test_an_old_orchestrator_without_a_digest_gets_a_clear_message(monkeypatch):
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: {"fleet": {}}})())
    with pytest.raises(SystemExit, match="too old"):
        da._board()


def _view():
    return {"counts": {"in_review": 1, "blocked": 1, "planned": 1, "cancelled": 1}, "closed_unscheduled": 7, "cancelled_hidden": 2,
            "sprints": [{"sprint": "2", "goal": "ship it", "started": "2026-10-30", "points_total": 5, "points_done": 2,
                         "stories": [{"id": "D-1", "title": "in sprint", "status": "in_review", "points": 3, "reason": "PR 4"},
                                     {"id": "D-2", "title": "stuck", "status": "blocked", "points": 2, "reason": "waiting on Stripe"}]},
                        {"sprint": "1", "stories": []}],
            "unscheduled": [{"id": "D-3", "title": "later", "status": "planned", "evidence": ""},
                            {"id": "D-4", "title": "stuck too", "status": "blocked", "evidence": ""}],
            "cancelled": [{"id": "D-5", "title": "dropped", "status": "cancelled", "evidence": "not needed"}]}


def test_backlog_is_arranged_per_sprint_with_the_reason_for_every_blocked_story(monkeypatch, capsys):
    import argparse
    board = {"projects": [{"project": "demo", "closed": 4, "backlog_view": _view()}], "digest": {}}
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: board})())
    da.cmd_backlog(argparse.Namespace(project="demo", limit=10, all=False))
    out = capsys.readouterr().out
    assert out.index("Sprint 2") < out.index("Not in a sprint yet") < out.index("Cancelled in the last 60 days")
    assert "Sprint 1" not in out                                                  # an empty sprint is hidden unless --all
    assert "why: waiting on Stripe" in out and "why: no reason recorded in the backlog" in out and "why: PR 4" not in out
    assert "7 closed ones not shown" in out and "2 older hidden" in out and "[not needed]" in out and "1 cancelled" in out
    da.cmd_backlog(argparse.Namespace(project="demo", limit=10, all=True))
    assert "Sprint 1" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="unknown project"):
        da.cmd_backlog(argparse.Namespace(project="nosuch", limit=5, all=False))


def test_cancel_needs_a_reason_and_a_confirmation_and_calls_the_cancel_path(monkeypatch, capsys):
    import argparse
    calls = []
    monkeypatch.setattr(da.sf, "cancel_story", lambda *a, **k: calls.append(a) or {"committed": True, "pushed": True, "push_error": None})
    ns = lambda **k: argparse.Namespace(**{"project": "demo", "story": "DM-1", "reason": "not needed", "yes": False, **k})   # noqa: E731
    with pytest.raises(SystemExit, match="needs --reason"):
        da.cmd_cancel(ns(reason="  "))
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    with pytest.raises(SystemExit, match="needs your confirmation"):
        da.cmd_cancel(ns())
    assert calls == []
    da.cmd_cancel(ns(yes=True))
    assert len(calls) == 1 and calls[0][2] == "DM-1" and calls[0][5] == "not needed"
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda p="": "n")
    da.cmd_cancel(ns())
    assert len(calls) == 1 and "left as it was" in capsys.readouterr().out


# ── sprint files follow approve / close / cancel ─────────────────────────────

def test_sprint_label_gets_a_stable_file_number_and_numbers_are_used_as_given(tmp_path, monkeypatch):
    (tmp_path / "viz" / "demo").mkdir(parents=True)
    (tmp_path / "viz" / "demo" / "sprint-04.md").write_text("x")
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path / "viz")
    assert da._sprint_number("demo", "S-roles") == 5 and da._sprint_number("demo", "S-roles") == 5      # remembered
    assert da._sprint_number("demo", "V2") == 6                                                          # next free, mapping counted
    assert da._sprint_number("demo", "9") == 9
    import json
    assert json.loads((tmp_path / "data" / "scrum" / "demo" / "sprints.json").read_text()) == {"S-roles": 5, "V2": 6}


def _approve_env(tmp_path, monkeypatch):
    import json
    d = tmp_path / "data" / "scrum" / "demo" / "S-x"
    d.mkdir(parents=True)
    (d / "stories.json").write_text(json.dumps({"request": "Let users pick the model", "stories": []}))
    plan = d / "S-1.plan.json"
    plan.write_text(json.dumps({"project": "demo", "sprint": "S-x", "story": {"id": "S-1", "title": "Pick", "points": 3}, "tasks": []}))
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path / "viz")
    (tmp_path / "viz" / "demo").mkdir(parents=True)
    ok = {"committed": True, "pushed": True, "push_error": None}
    return plan, ok


def test_approve_registers_the_story_then_opens_it_in_the_sprint_file_once(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    plan, ok = _approve_env(tmp_path, monkeypatch)
    calls = {"reg": 0, "open": []}
    monkeypatch.setattr(da.sf, "register_story", lambda *a, **k: calls.__setitem__("reg", calls["reg"] + 1) or dict(ok, id="DM-002"))
    monkeypatch.setattr(da.sf, "sprint_open_story", lambda *a, **k: calls["open"].append(a) or ok)
    da.cmd_approve(argparse.Namespace(plan=str(plan)))
    assert "sprint-01.md now holds DM-002 (committed, pushed)" in capsys.readouterr().out
    saved = json.loads(plan.read_text())
    assert saved["backlog_id"] == "DM-002" and saved["sprint_number"] == 1 and "approval" in saved
    (_repo, project, n, story, bid, request, day), = calls["open"]
    assert (project, n, bid, request, story["points"]) == ("demo", 1, "DM-002", "Let users pick the model", 3)
    da.cmd_approve(argparse.Namespace(plan=str(plan)))                                    # approving again: nothing is repeated
    assert calls["reg"] == 1 and len(calls["open"]) == 1


def test_a_failing_sprint_file_update_never_breaks_approve(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    plan, ok = _approve_env(tmp_path, monkeypatch)
    monkeypatch.setattr(da.sf, "register_story", lambda *a, **k: dict(ok, id="DM-002"))

    def boom(*a, **k):
        raise da.sf.PlanError("sprint-01.md is not a dis-agent sprint")
    monkeypatch.setattr(da.sf, "sprint_open_story", boom)
    da.cmd_approve(argparse.Namespace(plan=str(plan)))
    out = capsys.readouterr().out
    assert "sprint file NOT updated: sprint-01.md is not a dis-agent sprint" in out and "approved" in out
    assert "sprint_number" not in json.loads(plan.read_text())                            # not recorded, so a later approve retries


def test_close_and_cancel_update_the_sprint_file_only_when_the_story_has_one(tmp_path, monkeypatch):
    import argparse
    import json
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path / "viz")
    marks = []
    ok = {"committed": True, "pushed": True, "push_error": None}
    monkeypatch.setattr(da.sf, "sprint_mark", lambda *a, **k: marks.append((a[2:5], k)) or ok)
    monkeypatch.setattr(da.sf, "cancel_story", lambda *a, **k: ok)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    da.cmd_cancel(argparse.Namespace(project="demo", story="DM-002", reason="not needed", yes=True))
    assert marks == []                                                                    # no dis-agent sprint holds it
    monkeypatch.setattr(da.sf, "find_sprint_of", lambda *a: 4)
    da.cmd_cancel(argparse.Namespace(project="demo", story="DM-002", reason="not needed", yes=True))
    (args, kw), = marks
    assert args[0] == 4 and args[1] == "DM-002" and args[2].startswith("❌ cancelled ") and kw["closing"] is True

    marks.clear()
    d = tmp_path / "data" / "scrum" / "demo" / "S"
    d.mkdir(parents=True)
    (d / "S-1.plan.json").write_text(json.dumps({"story": {"id": "S-1", "title": "t"}, "backlog_id": "DM-002", "sprint_number": 4, "tasks": []}))
    p = lambda role: {"project": "demo", "sprint": "S", "story": "S-1", "role": role}   # noqa: E731
    rows = [{"id": "00000001x", "status": "done", "agent": "claude", "created_at": "1", "payload": p("dev_frontend"), "result": {}},
            {"id": "00000002x", "status": "done", "agent": "agy", "created_at": "2", "payload": p("tester"),
             "result": {"response": '{"validation":{"criteria":[{"n":1,"status":"pass","evidence":"t"}]}}'}},
            {"id": "00000003x", "status": "done", "agent": "codex", "created_at": "3", "payload": p("reviewer"), "result": {"response": "ok"}}]
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: rows})())
    monkeypatch.setattr(da.sf, "close_story", lambda *a, **k: ok)
    da.cmd_close(argparse.Namespace(project="demo", sprint="S", story=None, yes=True, force=False))
    (args, kw), = marks
    assert args[0] == 4 and args[1] == "DM-002" and args[2].startswith("✅ done ") and kw["closing"] is True


# ── sprint label vs number, default sprint, validate ─────────────────────────

def _plans(tmp_path, monkeypatch, label="S-roles", number=1):
    import json
    base = tmp_path / "data" / "scrum" / "demo"
    (base / label).mkdir(parents=True)
    (base / label / "S-1.plan.json").write_text(json.dumps({"story": {"id": "S-1", "title": "Pick", "acceptance": ["a", "b"]}, "backlog_id": "DM-002", "tasks": []}))
    (base / "sprints.json").write_text(json.dumps({label: number}))
    monkeypatch.setattr(da, "ROOT", tmp_path)
    (tmp_path / "viz" / "demo").mkdir(parents=True)
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path / "viz")
    return base


def test_the_sprint_can_be_named_by_label_or_by_the_number_the_board_shows(tmp_path, monkeypatch):
    _plans(tmp_path, monkeypatch)
    assert da.resolve_sprint("demo", "S-roles") == "S-roles"
    assert da.resolve_sprint("demo", "1") == "S-roles" and da.resolve_sprint("demo", "01") == "S-roles"
    for bad in ("S-1", "2"):                                                    # S-1 is a STORY id, 2 has no sprint
        with pytest.raises(SystemExit) as e:
            da.resolve_sprint("demo", bad)
        assert "Sprints with plans: S-roles" in str(e.value) and "label or the number the board shows" in str(e.value)
    with pytest.raises(SystemExit, match="Sprints with plans: none"):
        da.resolve_sprint("nosuch", "1")


def test_po_and_planning_default_to_the_next_free_sprint_number(tmp_path, monkeypatch, capsys):
    import argparse
    _plans(tmp_path, monkeypatch)
    (tmp_path / "viz" / "demo" / "sprint-03.md").write_text("x")
    assert da.next_sprint("demo") == "4" and da.next_sprint("nosuch") == "1"
    ns = argparse.Namespace(project="demo", sprint=None)
    da._default_sprint(ns)
    assert ns.sprint == "4" and "using sprint 4" in capsys.readouterr().out
    ns = argparse.Namespace(project="demo", sprint="7")
    da._default_sprint(ns)
    assert ns.sprint == "7"                                                     # an explicit one is left alone


def test_validate_queues_the_tester_prints_the_verdict_and_says_when_it_is_not_a_pass(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    base = _plans(tmp_path, monkeypatch)
    monkeypatch.setattr(da, "projects_cfg", lambda: {"machines": {}, "projects": {"demo": {"repo": "git@github.com:o/demo.git", "dir": "demo"}}})
    queued = []
    verdict = ('{"validation":{"suite":"9 passed, 0 failed","criteria":[{"n":1,"status":"pass","evidence":"t1"},'
               '{"n":2,"status":"unverified","evidence":"needs a live system"}]}}')

    class C:
        def queue(self, payload, notes):
            queued.append(payload)
            return {"id": "tt1"}

        def wait(self, ids, **k):
            return {"tt1": {"status": "done", "agent": "agy", "result": {"response": verdict}}}
    monkeypatch.setattr(sc, "client", lambda: C())
    da.cmd_validate(argparse.Namespace(project="demo", sprint="1", story="DM-002", approved=False, branch="main"))
    out = capsys.readouterr().out
    assert "1 pass, 0 fail, 1 unverified" in out and "needs a live system" in out and "close will refuse" in out
    q = queued[0]
    assert "git@github.com:o/demo.git" in q["prompt"]
    assert q["role"] == "tester" and q["story"] == "S-1" and q["sprint"] == "S-roles" and "already merged" in q["prompt"] and "dis/S-1-validation" in q["prompt"]
    assert json.loads((base / "S-roles" / "validation-S-1.json").read_text())["counts"]["unverified"] == 1
    with pytest.raises(SystemExit, match="no plan for DM-9"):
        da.cmd_validate(argparse.Namespace(project="demo", sprint="S-roles", story="DM-9", approved=False, branch=None))


def test_close_accepts_the_board_number_and_explains_a_wrong_sprint(tmp_path, monkeypatch, capsys):
    import argparse
    _plans(tmp_path, monkeypatch)
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: []})())
    da.cmd_close(argparse.Namespace(project="demo", sprint="1", story=None, yes=True, force=False))
    out = capsys.readouterr().out
    assert "DM-002 (S-1)" in out and "not every task is done" in out                      # found the plan through the number
    with pytest.raises(SystemExit, match="Sprints with plans: S-roles"):
        da.cmd_close(argparse.Namespace(project="demo", sprint="S-1", story=None, yes=True, force=False))


def test_close_check_shows_the_evidence_and_never_closes_or_prompts(tmp_path, monkeypatch, capsys):
    pass_ = '{"validation":{"criteria":[{"n":1,"status":"pass","evidence":"t1"}]}}'
    closed, out = _close_with(tmp_path, monkeypatch, pass_, capsys, check=True)
    assert closed == [] and "CHECK ONLY: every gate passes" in out and "1 pass, 0 fail" in out
    closed, out = _close_with(tmp_path, monkeypatch, None, capsys, check=True)
    assert closed == [] and "NOT closing" in out and "CHECK ONLY" not in out              # a failing gate is reported, not previewed as closable


def test_close_takes_the_story_by_plan_id_or_backlog_id(tmp_path, monkeypatch, capsys):
    import argparse
    _plans(tmp_path, monkeypatch)
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: []})())
    for ident in ("S-1", "DM-002"):
        da.cmd_close(argparse.Namespace(project="demo", sprint="S-roles", story=[ident], yes=False, force=False, check=True))
        assert "DM-002 (S-1)" in capsys.readouterr().out, ident
    da.cmd_close(argparse.Namespace(project="demo", sprint="S-roles", story=["DM-777"], yes=False, force=False, check=True))
    assert "DM-777: no registered plan" in capsys.readouterr().out


# ── dis-agent next ───────────────────────────────────────────────────────────

def test_local_plans_report_approval_and_whether_the_plan_ran(tmp_path, monkeypatch):
    import json
    from shared import scrum_flow
    monkeypatch.setattr(da, "ROOT", tmp_path)
    d = tmp_path / "data" / "scrum" / "demo" / "S"
    d.mkdir(parents=True)
    plan = {"project": "demo", "sprint": "S", "story": {"id": "S-1", "title": "T"}, "tasks": [{"key": "t1"}], "backlog_id": "DM-1", "sprint_number": 2}
    f = d / "S-1.plan.json"
    f.write_text(json.dumps(plan))
    got = da._local_plans()
    assert got == [{"project": "demo", "sprint": "S", "sprint_number": 2, "story_id": "S-1", "backlog_id": "DM-1", "title": "T",
                    "approved": False, "ran": False, "path": "data/scrum/demo/S/S-1.plan.json"}]
    scrum_flow.approve_plan(f, "me", "2026-10-31T00:00:00+00:00")
    f.with_suffix(".run.json").write_text("[]")
    got = da._local_plans()[0]
    assert got["approved"] is True and got["ran"] is True
    (d / "broken.plan.json").write_text("{not json")
    assert len(da._local_plans()) == 1                                           # an unreadable plan file is skipped, not fatal


def test_next_prints_ranked_suggestions_or_json_and_reads_the_grooming_reports(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    monkeypatch.setattr(da, "ROOT", tmp_path)
    g = tmp_path / "data" / "scrum" / "demo" / "grooming"
    g.mkdir(parents=True)
    (g / "groom-2026-10-31.json").write_text("{}")
    board = {"projects": [{"project": "demo", "backlog_view": {"sprints": [], "unscheduled": [{"id": "D-1", "status": "planned", "evidence": ""}], "cancelled": []}}],
             "digest": {"review_queue": [], "decisions": [], "opus": None}}
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: board})())
    da.cmd_next(argparse.Namespace(project=None, limit=6, json=False))
    out = capsys.readouterr().out
    assert "demo has 1 open story and no sprint running (groomed 2026-10-31)" in out and "command: /sprint demo" in out
    da.cmd_next(argparse.Namespace(project="demo", limit=6, json=True))
    data = json.loads(capsys.readouterr().out)
    assert data[0]["command"] == "/sprint demo" and data[0]["kind"].startswith("queues agent work")


# ── the board refreshes right after a change ─────────────────────────────────

def _git_repo_with_commit(tmp_path):
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "c"], check=True, env=env)
    return subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_after_a_pushed_change_the_board_is_refreshed_and_the_user_is_told_if_it_has_the_commit(tmp_path, monkeypatch, capsys):
    head = _git_repo_with_commit(tmp_path)
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path)
    calls = []

    def client_returning(h):
        return lambda: type("C", (), {"_call": lambda self, m, p, **k: calls.append((m, p)) or {"head": h}})()
    ok = {"committed": True, "pushed": True, "push_error": None}
    monkeypatch.setattr(sc, "client", client_returning(head))
    da._report("DM-1 ACCEPTED", ok)
    out = capsys.readouterr().out
    assert calls == [("POST", "/scrum/refresh")] and f"refreshed, it is now reading {head}: your change is visible" in out
    monkeypatch.setattr(sc, "client", client_returning("deadbee"))
    da._report("DM-1 ACCEPTED", ok)
    out = capsys.readouterr().out
    assert "reads deadbee, not " + head in out and "catches up within ~3 minutes" in out and "visible" not in out


def test_no_refresh_when_nothing_was_pushed_and_a_failed_refresh_never_looks_like_a_failed_change(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: calls.append(a)})())
    da._report("x", {"committed": True, "pushed": False, "push_error": ["offline"]})
    assert calls == [] and "NOT pushed" in capsys.readouterr().out                       # unpushed: the board cannot have it, so do not pretend

    def boom():
        raise RuntimeError("no orchestrator")
    monkeypatch.setattr(sc, "client", boom)
    da._report("x", {"committed": True, "pushed": True, "push_error": None})
    out = capsys.readouterr().out
    assert "committed, pushed" in out and "could not be refreshed now (RuntimeError)" in out
