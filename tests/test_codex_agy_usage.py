"""codex and agy put the usage their CLI printed into result["usage"]; cost stays None, nothing printed stays absent."""
import asyncio
import json

from agents import agy_agent, codex_agent
from shared import tracking


def test_codex_tokens_used_line():
    assert codex_agent.parse_tokens_used("answer\ntokens used\n4,071\n") == 4071
    assert codex_agent.parse_tokens_used("tokens used: 10\n...\ntokens used\n25") == 25   # last wins
    assert codex_agent.parse_tokens_used("nothing here") is None


def test_codex_usage_reaches_task_columns_without_cost():
    cols = tracking.from_result({"agent": "codex", "usage": {"tokens_total": 4071}})
    assert cols["tokens_total"] == 4071 and "cost_usd" not in cols


def test_agy_json_usage_no_cost():
    out = json.dumps({"response": " hi ", "status": "SUCCESS",
                      "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 40}})
    text, usage, failed = agy_agent.parse_json_output(out)
    assert (text, failed) == ("hi", False)
    assert usage == {"tokens_in": 10, "tokens_out": 5, "tokens_total": 40}   # no cost printed: key absent
    assert "cost_usd" not in tracking.from_result({"agent": "agy", "usage": usage})   # None, not 0


def test_agy_prints_nothing():
    for out in ("plain answer", "", '{"response": "x"}', '{"response": "x", "usage": {}}', "null"):
        assert agy_agent.parse_json_output(out)[1] is None


def test_agy_failure_keeps_usage(monkeypatch):
    out = json.dumps({"response": "boom", "status": "ERROR", "usage": {"input_tokens": 3, "output_tokens": 1}})

    class P:
        returncode = 1
        async def communicate(self):
            return out.encode(), b""

    async def fake(*a, **k):
        return P()
    monkeypatch.setattr(agy_agent, "_find_cli", lambda: "/bin/agy")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    r = asyncio.run(agy_agent.run("x"))
    assert r["ok"] is False and r["usage"]["tokens_in"] == 3
