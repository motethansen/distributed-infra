"""S-5: the desk lists pending responses under their item (comment wins), item_missing, consume once. No network."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "clients" / "fleet-mcp")]
import dis_agent as da  # noqa: E402
import scrum_cli as sc  # noqa: E402
from shared import scrum_flow as sf  # noqa: E402
from shared import scrum_responses as sr  # noqa: E402

DIGEST = {"review_queue": [{"item_key": "demo:DM-001"}], "decisions": [{"item_key": "stall:demo"}], "adjustments": [{"item_key": "adjust:x"}]}
ROWS = [
    {"id": 1, "kind": "response", "item_key": "demo:DM-001", "actor": "michael", "created_at": "2026-11-06T01:00:00Z", "action": "confirm",
     "suggestion_text": "Ship it", "comment": None},
    {"id": 2, "item_key": "stall:demo", "actor": "michael", "created_at": "2026-11-06T02:00:00Z", "action": "comment",
     "suggestion_text": "old", "comment": "my words"},
    {"id": 3, "kind": "response", "item_key": "gone:G-1", "actor": "michael", "created_at": "2026-11-06T03:00:00Z", "action": "comment", "comment": "orphan"},
    {"id": 4, "kind": "command", "item_key": "demo:DM-001", "actor": "michael", "created_at": "t", "action": "comment", "comment": "run it"},
]


class FakeClient:
    """Stands in for the FleetClient: records calls, never touches a network."""
    def __init__(self, rows=ROWS, consumed=()):
        self.rows, self.consumed, self.calls = rows, set(consumed), []

    def _call(self, method, path, headers=None, **kw):
        self.calls.append((method, path, headers, kw))
        if method == "GET":
            return self.rows
        rid = int(path.split("/")[3])
        if rid in self.consumed:
            raise sf.PlanError(f"POST {path}: HTTP 409 already consumed")
        self.consumed.add(rid)
        return {"id": rid, "status": "consumed"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse():
        raise RuntimeError("tests must not reach the live orchestrator")
    monkeypatch.setattr(sc, "client", refuse)


def test_only_response_rows_are_listed_and_command_rows_are_not():
    rows, err = sr.fetch_pending(FakeClient())
    assert err is None and [r["id"] for r in rows] == [1, 2, 3]


def test_comment_wins_over_suggestion_text_and_each_response_sits_under_its_item():
    rows, _ = sr.fetch_pending(FakeClient())
    out = "\n".join(sr.render(rows, sr.board_item_keys(DIGEST)))
    assert "demo:DM-001:\n  [1] michael @ 2026-11-06T01:00:00Z (confirm): Ship it" in out
    assert "(comment): my words" in out and "old" not in out


def test_a_response_whose_item_left_the_board_is_item_missing_not_dropped():
    rows, _ = sr.fetch_pending(FakeClient())
    out = sr.render(rows, sr.board_item_keys(DIGEST))
    assert "item_missing:" in out and any("[3]" in l and "orphan" in l for l in out[out.index("item_missing:"):])


def test_consume_sends_the_actor_header_and_a_second_call_says_already_consumed():
    c = FakeClient()
    assert sr.consume(c, 1, "michael") == (True, "consumed 1")
    assert c.calls[0][:3] == ("POST", "/scrum/responses/1/consume", {"X-Actor": "dis-agent:michael"})
    assert sr.consume(c, 1, "michael") == (False, "already consumed")


def test_cli_consume_exits_non_zero_on_the_second_call(monkeypatch, capsys):
    c = FakeClient()
    monkeypatch.setattr(sc, "client", lambda: c)
    monkeypatch.setenv("USER", "michael")
    da.cmd_responses(SimpleNamespace(consume=7))
    with pytest.raises(SystemExit) as e:
        da.cmd_responses(SimpleNamespace(consume=7))
    assert e.value.code == 1 and "already consumed" in capsys.readouterr().out


def test_next_prints_the_responses_section(monkeypatch, capsys):
    monkeypatch.setattr(sc, "client", lambda: FakeClient())
    monkeypatch.setattr(da, "_board", lambda: {"digest": DIGEST})
    da._print_responses()
    out = capsys.readouterr().out
    assert "Ship it" in out and "item_missing:" in out and "run it" not in out


def test_an_unavailable_endpoint_does_not_break_the_desk():
    class Down:
        def _call(self, *a, **k):
            raise sf.PlanError("no orchestrator reachable")
    assert "unavailable" in sr.section(Down(), DIGEST)


def test_next_prints_responses_unavailable_when_the_orchestrator_is_unreachable(capsys):
    da._print_responses()  # sc.client is refused by the fixture
    assert "responses unavailable" in capsys.readouterr().out
