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


def _close_with(tmp_path, monkeypatch, tester_response, capsys):
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
    da.cmd_close(argparse.Namespace(project="demo", sprint="S", story=None, yes=True, force=False))
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


def test_review_without_a_project_lists_the_queue_and_with_one_needs_a_sprint(monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: _board_with(
        [{"project": "p", "id": "P-1", "title": "A thing", "evidence": "validation ok"}])})())
    da.cmd_review(argparse.Namespace(project=None, sprint=None))
    out = capsys.readouterr().out
    assert "1 stories wait for your sprint review" in out and "P-1" in out and "validation ok" in out
    with pytest.raises(SystemExit, match="-s SPRINT"):
        da.cmd_review(argparse.Namespace(project="p", sprint=None))
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: _board_with()})())
    da.cmd_review(argparse.Namespace(project=None, sprint=None))
    assert "Nothing is waiting" in capsys.readouterr().out


def test_an_old_orchestrator_without_a_digest_gets_a_clear_message(monkeypatch):
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: {"fleet": {}}})())
    with pytest.raises(SystemExit, match="too old"):
        da._board()


def test_backlog_lists_only_open_rows_with_in_flight_work_first(tmp_path, monkeypatch, capsys):
    import argparse
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "product-backlog.md").write_text(
        "| ID | Story | Priority | Status | Evidence |\n|---|---|---|---|---|\n"
        "| DM-001 | planned one | High | 🔵 planned | |\n| DM-002 | done one | High | ✅ done 2026-01-01 | |\n"
        "| DM-003 | in review one | High | 🔍 in review | PR |\n| DM-004 | **running** | High | 🏃 in progress | |\n| DM-005 | stuck | Low | 🚧 blocked | |\n")
    monkeypatch.setattr(da, "viz_repo", lambda: tmp_path)
    da.cmd_backlog(argparse.Namespace(project="demo", limit=3))
    out = capsys.readouterr().out
    ids = [l.split()[0] for l in out.splitlines() if l.strip().startswith("DM-")]
    assert ids == ["DM-004", "DM-003", "DM-005"] and "4 open stories" in out and "1 more" in out and "DM-002" not in out
    with pytest.raises(SystemExit, match="not found"):
        da.cmd_backlog(argparse.Namespace(project="nosuch", limit=5))
