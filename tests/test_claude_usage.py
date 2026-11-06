"""parse_json_output keeps the usage the Claude CLI printed, on success and on failure."""
import asyncio
import json

from agents import claude_agent
from agents.claude_agent import parse_json_output

KEYS = {"tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write", "cost_usd", "model", "agent"}

SUCCESS = {
    "type": "result", "subtype": "success", "is_error": False, "result": " done \n",
    "total_cost_usd": 0.0421,
    "usage": {"input_tokens": 12, "cache_creation_input_tokens": 3400, "cache_read_input_tokens": 51000,
              "output_tokens": 870},
    "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 12, "outputTokens": 870, "costUSD": 0.04},
                   "claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 9, "costUSD": 0.002}},
}
ERROR = {
    "type": "result", "subtype": "error_during_execution", "is_error": True, "result": "tool blew up",
    "total_cost_usd": 0.01,
    "usage": {"input_tokens": 40, "output_tokens": 5},
    "model": "claude-sonnet-5-5",
}


def test_success_every_field():
    text, usage, is_error = parse_json_output(json.dumps(SUCCESS))
    assert (text, is_error) == ("done", False)
    assert set(usage) == KEYS
    assert usage == {"tokens_in": 12, "tokens_cache_write": 3400, "tokens_cache_read": 51000, "tokens_out": 870,
                     "cost_usd": 0.0421, "model": "claude-sonnet-5-5", "agent": "claude"}


def test_is_error_keeps_usage():
    text, usage, is_error = parse_json_output(json.dumps(ERROR))
    assert is_error is True and text == "tool blew up"
    assert usage["tokens_in"] == 40 and usage["tokens_out"] == 5 and usage["cost_usd"] == 0.01
    assert usage["model"] == "claude-sonnet-5-5"
    assert usage["tokens_cache_read"] is None and usage["tokens_cache_write"] is None   # missing is None, not 0


def test_plain_text_gives_none_without_raising():
    for out in ("just an answer", "", "{not json", "[1, 2]", '{"no_result": 1}', "null"):
        text, usage, is_error = parse_json_output(out)
        assert text == out and is_error is False
        assert set(usage) == KEYS
        assert {k: v for k, v in usage.items() if k != "agent"} == {k: None for k in KEYS - {"agent"}}
        assert usage["agent"] == "claude"


def test_odd_values_never_raise():
    d = {"result": "x", "usage": {"input_tokens": "lots", "output_tokens": None}, "total_cost_usd": "free",
         "modelUsage": {"m": "junk"}}
    _, usage, _ = parse_json_output(json.dumps(d))
    assert usage["tokens_in"] is None and usage["tokens_out"] is None and usage["cost_usd"] is None
    assert usage["model"] == "m"


class _Proc:
    def __init__(self, out, rc):
        self._out, self.returncode = out, rc

    async def communicate(self):
        return self._out.encode(), b""


def _run(monkeypatch, out, rc):
    async def fake(*a, **k):
        return _Proc(out, rc)
    monkeypatch.setattr(claude_agent, "_find_cli", lambda: "/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return asyncio.run(claude_agent.run("hi"))


def test_run_success_carries_usage(monkeypatch):
    r = _run(monkeypatch, json.dumps(SUCCESS), 0)
    assert r["ok"] and r["usage"]["tokens_cache_read"] == 51000


def test_run_failure_keeps_usage_and_behaviour(monkeypatch):
    r = _run(monkeypatch, json.dumps(ERROR), 1)
    assert r["ok"] is False and r["error"] == "tool blew up" and r["agent"] == "claude"
    assert r["usage"]["tokens_in"] == 40


def test_run_plain_text_has_no_usage_key(monkeypatch):
    r = _run(monkeypatch, "plain answer", 0)
    assert r["ok"] and r["response"] == "plain answer" and "usage" not in r
