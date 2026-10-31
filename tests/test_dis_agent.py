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
             "payload": {"project": "demo", "sprint": "S", "story": "S-1", "role": "reviewer"}, "result": {"response": "ok"}}]

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
