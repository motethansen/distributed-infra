"""Failover between LLM agents when one is unavailable (agents/failover.py + runner).

The agents are replaced by fakes, so nothing here calls a real provider.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agents import failover as fo
from agents import runner
from agents.router import route_chain

CLAUDE_LIMIT = "You've hit your session limit · resets 8:50pm (Asia/Singapore)"


@pytest.fixture(autouse=True)
def _isolated_cooldowns(tmp_path, monkeypatch):
    monkeypatch.setattr(fo, "COOLDOWN_FILE", tmp_path / "cooldowns.json")


def _fake_agents(monkeypatch, behaviour: dict):
    """behaviour: agent -> response dict (or callable(kwargs) -> dict). Records calls."""
    calls = []

    def make(name):
        async def run(**kwargs):
            calls.append((name, kwargs))
            b = behaviour[name]
            return b(kwargs) if callable(b) else dict(b)
        return run

    monkeypatch.setattr(runner, "_load_agents", lambda: None)
    monkeypatch.setattr(runner, "AGENTS", {n: make(n) for n in
                                           ("claude", "codex", "agy", "deepseek", "content", "social")})
    return calls


OK = lambda name: {"ok": True, "agent": name, "response": f"hello from {name}"}  # noqa: E731
LIMITED = {"ok": False, "agent": "claude", "error": CLAUDE_LIMIT}


# ── classification + reset parsing ─────────────────────────────────────────────
@pytest.mark.parametrize("err,kind", [
    (CLAUDE_LIMIT, "limit"),
    ("You've hit your usage limit. Try again in 2 hours 3 minutes.", "limit"),
    ("429 Too Many Requests", "limit"),
    ("RESOURCE_EXHAUSTED: Quota exceeded for model", "limit"),
    ("DeepSeek API error 402: Insufficient Balance", "limit"),
    ("Overloaded", "limit"),
    ("claude CLI not found. Install: npm install -g …", "unavailable"),
    ("Not logged in · Please run /login", "unavailable"),
    ("claude CLI timed out after 1800s", None),
    ("AssertionError: 3 tests failed", None),
    ("", None),
])
def test_classify(err, kind):
    assert fo.classify(err) == kind


def test_parse_reset_clock_time_in_named_zone():
    now = datetime(2026, 9, 6, 9, 41, tzinfo=timezone.utc)  # 17:41 in Singapore
    until = fo.parse_reset(CLAUDE_LIMIT, now)
    assert until == datetime(2026, 9, 6, 12, 50, tzinfo=timezone.utc)  # 20:50 SGT


def test_parse_reset_rolls_to_tomorrow_when_time_passed():
    now = datetime(2026, 9, 6, 13, 0, tzinfo=timezone.utc)  # 21:00 SGT, after 20:50
    assert fo.parse_reset(CLAUDE_LIMIT, now) == datetime(2026, 9, 7, 12, 50, tzinfo=timezone.utc)


def test_parse_reset_relative_duration():
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    assert fo.parse_reset("Try again in 2 hours 3 minutes.", now) == now + timedelta(hours=2, minutes=3)


def test_parse_reset_unknown_returns_none():
    assert fo.parse_reset("quota exceeded") is None


def test_mark_limited_defaults_and_expires():
    now = datetime.now(timezone.utc)
    until = fo.mark_limited("codex", "429 Too Many Requests", now)
    assert until == now + timedelta(seconds=fo.DEFAULT_COOLDOWN_SECS)
    assert fo.cooling_until("codex", now) == until
    assert fo.cooling_until("codex", until + timedelta(seconds=1)) is None
    fo.clear("codex")
    assert fo.cooling_until("codex", now) is None


# ── routing chain ──────────────────────────────────────────────────────────────
def test_chain_for_coding_fails_over_to_agy_then_codex():
    # coding writes files: agy can, codex is read-only here, so it goes last
    assert [a for a, _ in route_chain(task_kind="code")] == ["claude", "agy", "codex"]


def test_chain_for_planning_fails_over_to_codex_then_agy():
    assert [a for a, _ in route_chain(task_kind="plan")] == ["claude", "codex", "agy"]


def test_privacy_chain_never_includes_deepseek():
    for kind in ("email", "finance", None):
        chain = route_chain(task_kind=kind, sensitivity="private", agent="deepseek")
        assert "deepseek" not in [a for a, _ in chain]


def test_pinned_agent_gets_default_fallbacks():
    chain = [a for a, _ in route_chain(agent="claude")]
    assert chain[0] == "claude" and "codex" in chain and len(chain) == len(set(chain))


# ── runner behaviour ───────────────────────────────────────────────────────────
def test_limit_fails_over_and_reports_it(monkeypatch):
    calls = _fake_agents(monkeypatch, {"claude": LIMITED, "codex": OK("codex"), "agy": OK("agy"),
                                       "deepseek": OK("deepseek")})
    r = asyncio.run(runner.run_agent(task_kind="plan", prompt="plan it"))
    assert r["ok"] and r["agent"] == "codex"
    assert [c[0] for c in calls] == ["claude", "codex"]
    assert r["failover"][0]["agent"] == "claude" and r["failover"][0]["reason"] == "limit"
    assert fo.cooling_until("claude") is not None


def test_cooling_agent_is_skipped_without_a_call(monkeypatch):
    fo.mark_limited("claude", CLAUDE_LIMIT)
    calls = _fake_agents(monkeypatch, {"claude": OK("claude"), "codex": OK("codex"), "agy": OK("agy"),
                                       "deepseek": OK("deepseek")})
    r = asyncio.run(runner.run_agent(agent="claude", prompt="hi"))
    assert [c[0] for c in calls] == ["codex"]
    assert r["agent"] == "codex" and r["failover"][0]["agent"] == "claude"


def test_all_cooling_still_tries_in_order(monkeypatch):
    for a in ("claude", "codex", "agy"):
        fo.mark_limited(a, "429")
    calls = _fake_agents(monkeypatch, {"claude": OK("claude"), "codex": OK("codex"), "agy": OK("agy"),
                                       "deepseek": OK("deepseek")})
    r = asyncio.run(runner.run_agent(task_kind="code", prompt="hi"))
    assert r["agent"] == "claude" and [c[0] for c in calls] == ["claude"]
    assert fo.cooling_until("claude") is None  # success clears the cooldown


def test_real_error_does_not_fail_over(monkeypatch):
    calls = _fake_agents(monkeypatch, {"claude": {"ok": False, "error": "3 tests failed"},
                                       "codex": OK("codex"), "agy": OK("agy"), "deepseek": OK("deepseek")})
    r = asyncio.run(runner.run_agent(task_kind="code", prompt="fix it"))
    assert not r["ok"] and [c[0] for c in calls] == ["claude"]


def test_failover_false_pins_the_agent(monkeypatch):
    calls = _fake_agents(monkeypatch, {"claude": LIMITED, "codex": OK("codex"), "agy": OK("agy"),
                                       "deepseek": OK("deepseek")})
    r = asyncio.run(runner.run_agent(agent="claude", prompt="hi", failover=False))
    assert not r["ok"] and [c[0] for c in calls] == ["claude"]


def test_every_agent_limited_says_so(monkeypatch):
    lim = {"ok": False, "error": "usage limit reached"}
    _fake_agents(monkeypatch, {"claude": lim, "codex": lim, "agy": lim, "deepseek": lim})
    r = asyncio.run(runner.run_agent(task_kind="code", prompt="hi"))
    assert not r["ok"] and "every agent in the chain is unavailable" in r["error"]


def test_fallback_gets_the_conversation_so_far(monkeypatch):
    calls = _fake_agents(monkeypatch, {"claude": LIMITED, "codex": OK("codex"), "agy": OK("agy"),
                                       "deepseek": OK("deepseek")})
    history = [{"role": "user", "text": "plan my trip to Kyoto"},
               {"role": "assistant", "text": "Day 1: Fushimi Inari", "agent": "claude"}]
    asyncio.run(runner.run_agent(agent="claude", prompt="and day 2?", session_id="s1",
                                 resume=True, history=history))
    codex_prompt = calls[1][1]["prompt"]
    assert "plan my trip to Kyoto" in codex_prompt and "Fushimi Inari" in codex_prompt
    assert codex_prompt.rstrip().endswith("and day 2?")
    assert "session_id" not in calls[1][1]  # codex is one-shot


def test_claude_catches_up_on_turns_it_missed(monkeypatch):
    calls = _fake_agents(monkeypatch, {"claude": OK("claude"), "codex": OK("codex"), "agy": OK("agy"),
                                       "deepseek": OK("deepseek")})
    history = [{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1", "agent": "claude"},
               {"role": "user", "text": "q2"}, {"role": "assistant", "text": "a2-by-codex", "agent": "codex"}]
    asyncio.run(runner.run_agent(agent="claude", prompt="q3", session_id="s1", resume=True, history=history))
    kw = calls[0][1]
    assert kw["session_id"] == "s1" and kw["resume"] is True
    assert "a2-by-codex" in kw["prompt"] and "a1" not in kw["prompt"]


def test_claude_rejoining_a_chat_it_never_answered_opens_fresh_session(monkeypatch):
    calls = _fake_agents(monkeypatch, {"claude": lambda kw: {"ok": True, "response": "hi",
                                                              "session_id": kw.get("session_id")},
                                       "codex": OK("codex"), "agy": OK("agy"), "deepseek": OK("deepseek")})
    history = [{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1", "agent": "codex"}]
    r = asyncio.run(runner.run_agent(agent="claude", prompt="q2", session_id="s1", resume=True, history=history))
    kw = calls[0][1]
    assert kw["resume"] is False and kw["session_id"] != "s1" and "a1" in kw["prompt"]
    assert r["session_id"] == kw["session_id"]


def test_plain_resume_without_history_is_unchanged(monkeypatch):
    calls = _fake_agents(monkeypatch, {"claude": OK("claude"), "codex": OK("codex"), "agy": OK("agy"),
                                       "deepseek": OK("deepseek")})
    asyncio.run(runner.run_agent(agent="claude", prompt="next", session_id="s1", resume=True))
    assert calls[0][1] == {"prompt": "next", "session_id": "s1", "resume": True}


def test_wrapper_agents_are_not_swapped(monkeypatch):
    calls = _fake_agents(monkeypatch, {"content": LIMITED, "claude": OK("claude"), "codex": OK("codex"),
                                       "agy": OK("agy"), "deepseek": OK("deepseek")})
    asyncio.run(runner.run_agent(agent="content", prompt="article"))
    assert [c[0] for c in calls] == ["content"]


def test_groq_is_gone():
    runner._load_agents()
    assert "groq" not in runner.AGENTS


def test_privacy_guard_strips_non_private_fallbacks_even_if_listed(monkeypatch):
    from agents import router
    policy = router._load_policy()
    policy = dict(policy, fallbacks=dict(policy.get("fallbacks", {}),
                                         privacy=[{"agent": "deepseek"}, {"agent": "codex"}]))
    monkeypatch.setattr(router, "_load_policy", lambda: policy)
    chain = [a for a, _ in router.route_chain(task_kind="finance")]
    assert chain == ["claude", "codex"]
