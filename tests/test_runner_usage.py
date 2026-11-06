"""Usage spent by failed-over attempts is summed into the final result and survives a failed task."""
import asyncio

from agents import runner


def test_add_usage_unknown_cost_stays_null_and_exposes_known_part():
    r = {"ok": True, "usage": {"tokens_in": 5, "cost_usd": None, "agent": "codex"}}
    runner.add_usage(r, [{"tokens_in": 7, "tokens_out": 2, "cost_usd": 0.5}])
    assert r["usage"] == {"tokens_in": 12, "tokens_out": 2, "cost_usd": None, "cost_complete": False,
                          "cost_usd_known_part": 0.5, "agent": "codex"}


def test_add_usage_sums_cost_when_every_attempt_is_known():
    r = {"ok": True, "usage": {"tokens_in": 5, "cost_usd": 0.25}}
    runner.add_usage(r, [{"tokens_in": 7, "cost_usd": 0.5}])
    assert r["usage"]["cost_usd"] == 0.75 and "cost_complete" not in r["usage"]


def test_add_usage_noop_without_spent():
    r = {"ok": True}
    runner.add_usage(r, [])
    assert "usage" not in r


def test_failover_sums_usage(monkeypatch):
    calls = iter([{"ok": False, "error": "usage limit reached", "usage": {"tokens_in": 4}},
                  {"ok": True, "response": "x", "usage": {"tokens_in": 6}}])

    async def one(*a, **k):
        return next(calls)
    monkeypatch.setattr(runner, "_run_one", one)
    from agents import failover as fo
    monkeypatch.setattr(fo, "cooling_until", lambda a, now=None: None)
    monkeypatch.setattr(fo, "mark_limited", lambda a, e: __import__("datetime").datetime.now())
    monkeypatch.setattr(fo, "clear", lambda a: None)
    r = asyncio.run(runner.run_agent(agent="claude", prompt="p"))
    assert r["ok"] and r["usage"]["tokens_in"] == 10


def test_failed_chain_keeps_usage_once(monkeypatch):
    async def one(*a, **k):
        return {"ok": False, "error": "weird crash", "usage": {"tokens_in": 4}}
    monkeypatch.setattr(runner, "_run_one", one)
    r = asyncio.run(runner.run_agent(agent="claude", prompt="p"))
    assert not r["ok"] and r["usage"]["tokens_in"] == 4
