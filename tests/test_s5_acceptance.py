"""S-5 acceptance (independent): the desk lists pending responses, item_missing, consume once. No network calls."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "clients" / "fleet-mcp")]
import dis_agent as da  # noqa: E402
import scrum_cli as sc  # noqa: E402
from shared import scrum_flow as sf  # noqa: E402

DIGEST = {"as_of": "2026-11-06", "review_queue": [{"item_key": "demo:DM-001"}], "decisions": [{"item_key": "stall:demo"}], "adjustments": []}
BOARD = {"digest": DIGEST, "fleet": {"totals": {"tasks": 1, "done": 1, "tokens_out": 1}}}
ROWS = [
    {"id": 1, "kind": "response", "item_key": "demo:DM-001", "actor": "alice", "created_at": "2026-11-06T01:00:00Z",
     "action": "confirm", "suggestion_text": "SUGGESTION-ONE", "comment": None},
    {"id": 2, "kind": "response", "item_key": "stall:demo", "actor": "bob", "created_at": "2026-11-06T02:00:00Z",
     "action": "comment", "suggestion_text": "SUGGESTION-TWO", "comment": "COMMENT-TWO"},
    {"id": 3, "kind": "response", "item_key": "gone:G-9", "actor": "carol", "created_at": "2026-11-06T03:00:00Z",
     "action": "comment", "suggestion_text": None, "comment": "ORPHAN-TEXT"},
    {"id": 4, "kind": "command", "item_key": "demo:DM-001", "actor": "dave", "created_at": "2026-11-06T04:00:00Z",
     "action": "comment", "suggestion_text": None, "comment": "COMMAND-TEXT"},
]


class FakeClient:
    key = "k"

    def __init__(self, rows=ROWS):
        self.rows, self.consumed, self.posts = rows, set(), []

    def _call(self, method, path, headers=None, **kw):
        if method == "GET" and path == "/scrum/sprints":
            return BOARD
        if method == "GET":
            p = kw.get("params") or {}
            rows = [r for r in self.rows if not (p.get("status") == "pending" and r["id"] in self.consumed)]
            if p.get("kind"):
                rows = [r for r in rows if r.get("kind") == p["kind"]]
            return list(reversed(rows))
        self.posts.append((path, headers))
        rid = int(path.split("/")[3])
        if rid in self.consumed:
            raise sf.PlanError(f"POST {path}: HTTP 409 already consumed")
        self.consumed.add(rid)
        return {"id": rid, "status": "consumed"}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    def refuse():
        raise RuntimeError("tests must not reach the live orchestrator")
    monkeypatch.setattr(sc, "client", refuse)
    monkeypatch.setenv("USER", "tester")
    from shared import scrum_next
    monkeypatch.setattr(scrum_next, "suggest", lambda *a, **k: [])
    monkeypatch.setattr(scrum_next, "render", lambda s: "NEXT")
    for n in ("_local_plans", "_local_proposals", "_local_human_steps"):
        monkeypatch.setattr(da, n, lambda *a, **k: {})
    monkeypatch.setattr(da, "_board", lambda: BOARD)
    from shared import scrum_digest
    monkeypatch.setattr(scrum_digest, "to_markdown", lambda d, fleet="": "# digest\n")


def _run(monkeypatch, c, *argv):
    monkeypatch.setattr(sc, "client", lambda: c)
    monkeypatch.setattr(sys, "argv", ["dis-agent", *argv])
    da.main()


def _scrum(monkeypatch, tmp_path, c, capsys):
    monkeypatch.setattr(da, "ROOT", tmp_path)
    _run(monkeypatch, c, "scrum")
    return capsys.readouterr().out


@pytest.mark.parametrize("cmd", ["next", "scrum"])
def test_c1_next_and_scrum_list_responses_under_item_with_actor_time_action_and_winning_text(monkeypatch, tmp_path, capsys, cmd):
    c = FakeClient()
    if cmd == "scrum":
        out = _scrum(monkeypatch, tmp_path, c, capsys)
    else:
        _run(monkeypatch, c, "next", "-p", "vantage")
        out = capsys.readouterr().out
    i1, i2 = out.index("demo:DM-001"), out.index("stall:demo")
    assert "alice" in out and "2026-11-06T01:00:00Z" in out and "confirm" in out and "SUGGESTION-ONE" in out[i1:]
    assert "bob" in out and "2026-11-06T02:00:00Z" in out and "COMMENT-TWO" in out[i2:]
    assert "SUGGESTION-TWO" not in out  # comment wins


@pytest.mark.parametrize("cmd", ["next", "scrum"])
def test_c2_response_for_item_not_on_board_is_item_missing_not_dropped(monkeypatch, tmp_path, capsys, cmd):
    c = FakeClient()
    if cmd == "scrum":
        out = _scrum(monkeypatch, tmp_path, c, capsys)
    else:
        _run(monkeypatch, c, "next", "-p", "vantage")
        out = capsys.readouterr().out
    assert "item_missing" in out and "ORPHAN-TEXT" in out and "gone:G-9" in out
    assert out.index("item_missing") < out.index("ORPHAN-TEXT")


def test_c3_responses_consume_posts_with_dis_agent_actor(monkeypatch, capsys):
    c = FakeClient()
    _run(monkeypatch, c, "responses", "--consume", "2")
    assert c.posts == [("/scrum/responses/2/consume", {"X-Actor": "dis-agent:tester"})]


def test_c3_second_consume_prints_already_consumed_and_exits_nonzero(monkeypatch, capsys):
    c = FakeClient()
    _run(monkeypatch, c, "responses", "--consume", "2")
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, c, "responses", "--consume", "2")
    assert e.value.code not in (0, None)
    cap = capsys.readouterr()
    assert "already consumed" in (cap.out + cap.err)
    assert len(c.posts) == 2


@pytest.mark.parametrize("cmd", ["next", "scrum"])
def test_c4_command_rows_are_not_listed_as_responses(monkeypatch, tmp_path, capsys, cmd):
    c = FakeClient()
    if cmd == "scrum":
        out = _scrum(monkeypatch, tmp_path, c, capsys)
    else:
        _run(monkeypatch, c, "next", "-p", "vantage")
        out = capsys.readouterr().out
    assert "COMMAND-TEXT" not in out and "dave" not in out
    assert "ORPHAN-TEXT" in out  # response rows from the same fixture are still shown


def test_c4_responses_command_does_not_list_or_consume_command_rows(monkeypatch, capsys):
    c = FakeClient()
    _run(monkeypatch, c, "responses")
    out = capsys.readouterr().out
    assert "COMMAND-TEXT" not in out
    assert all("/4/" not in p for p, _ in c.posts)


def test_c5_fixture_refuses_the_live_client():
    with pytest.raises(RuntimeError, match="live orchestrator"):
        sc.client()
