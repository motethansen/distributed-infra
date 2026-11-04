"""Independent acceptance tests for story S-2: Every agent_run result says who actually answered, with what model, and why it changed."""
from __future__ import annotations

import asyncio
import importlib

import pytest

from agents import failover as fo
from agents import roles
from agents import runner
from shared.models import Task, TaskStatus, TaskType
from worker.handlers import agent_run

CLAUDE_LIMIT = "You've hit your session limit · resets 8:50pm (Asia/Singapore)"


@pytest.fixture(autouse=True)
def _isolated_cooldowns(tmp_path, monkeypatch):
    monkeypatch.setattr(fo, "COOLDOWN_FILE", tmp_path / "cooldowns.json")


def _fake_agents(monkeypatch, behaviour: dict):
    """Replace runner.AGENTS with fake agents returning configured responses."""
    calls = []

    def make(name):
        async def run(**kwargs):
            calls.append((name, kwargs))
            b = behaviour[name]
            return b(kwargs) if callable(b) else dict(b)
        return run

    monkeypatch.setattr(runner, "_load_agents", lambda: None)
    monkeypatch.setattr(
        runner,
        "AGENTS",
        {n: make(n) for n in ("claude", "codex", "agy", "deepseek", "content", "social")},
    )
    return calls


def _ok_agent(name: str, model: str | None = None):
    def _fn(kwargs):
        m = kwargs.get("model") or model or f"{name}-default"
        return {"ok": True, "agent": name, "model": m, "response": f"hello from {name}"}
    return _fn


LIMITED_CLAUDE = {"ok": False, "agent": "claude", "error": CLAUDE_LIMIT}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "k")
    import orchestrator.db as db_mod
    importlib.reload(db_mod)
    import orchestrator.main as m
    importlib.reload(m)
    return m


async def _queue_and_run(api, payload: dict) -> dict:
    """Queue an agent_run task through the orchestrator API and run it through handle_agent_run."""
    task = await api.create_task(
        api.TaskCreate(type=TaskType.agent_run, payload=dict(payload)),
        x_secret_key="k",
        x_client="test",
    )
    res = await agent_run.handle_agent_run(task)
    await api.db.update_task(task.id, {"status": TaskStatus.done, "result": res})
    done_task = await api.db.get_task(task.id)
    return done_task.result


# ── Criterion 1 ──────────────────────────────────────────────────────────────
# Every finished agent_run task, whether direct or routed, and with or without
# failover, has result.answered_by (agent), result.answered_model,
# result.requested (the choice id or {agent, model, role} as queued) and
# result.failover (a list, empty when there was none). Today only content_agent
# sets answered_by; runner.py is changed to set it for every agent.


@pytest.mark.asyncio
async def test_c1_every_finished_agent_run_has_answered_by_answered_model_requested_and_failover(api, monkeypatch):
    _fake_agents(
        monkeypatch,
        {
            "claude": _ok_agent("claude", "sonnet"),
            "codex": _ok_agent("codex", "gpt-5"),
            "agy": _ok_agent("agy", "gemini-2.5-pro"),
            "deepseek": _ok_agent("deepseek", "deepseek-chat"),
            "content": {"ok": True, "agent": "content", "answered_by": "claude", "model": "sonnet", "response": "post"},
            "social": _ok_agent("social", "sonnet"),
        },
    )

    # 1) Direct task without failover
    r_direct = await _queue_and_run(api, {"agent": "claude", "model": "sonnet", "prompt": "hi"})
    assert r_direct["answered_by"] == "claude"
    assert r_direct["answered_model"] == "sonnet"
    assert r_direct["requested"] is not None
    assert r_direct["failover"] == []

    # 2) Routed task (auto / no agent specified) without failover
    r_routed = await _queue_and_run(api, {"task_kind": "plan", "prompt": "plan sprint"})
    assert r_routed["answered_by"] == "claude"
    assert isinstance(r_routed["answered_model"], str) and r_routed["answered_model"]
    assert r_routed["requested"] is not None
    assert r_routed["failover"] == []

    # 3) Role-queued task without failover
    r_role = await _queue_and_run(api, {"role": "scrum_master", "prompt": "review board"})
    assert r_role["answered_by"] == "claude"
    assert r_role["answered_model"] == "sonnet"
    assert r_role["requested"] is not None
    assert r_role["failover"] == []

    # 4) Runner.run_agent directly sets answered_by, answered_model, requested, failover for every agent
    for agent_name in ("claude", "codex", "agy", "deepseek"):
        r_run = await runner.run_agent(agent=agent_name, prompt="hello")
        assert r_run["answered_by"] == agent_name
        assert isinstance(r_run.get("answered_model"), str) and r_run["answered_model"]
        assert "requested" in r_run and r_run["requested"] is not None
        assert r_run.get("failover") == []

    # 5) Task with failover
    _fake_agents(
        monkeypatch,
        {
            "claude": LIMITED_CLAUDE,
            "codex": _ok_agent("codex", "gpt-5"),
            "agy": _ok_agent("agy", "gemini-2.5-pro"),
            "deepseek": _ok_agent("deepseek", "deepseek-chat"),
        },
    )
    r_fo = await _queue_and_run(api, {"agent": "claude", "model": "sonnet", "prompt": "hi"})
    assert r_fo["answered_by"] == "codex"
    assert r_fo["answered_model"] == "gpt-5"
    assert r_fo["requested"] is not None
    assert isinstance(r_fo["failover"], list) and len(r_fo["failover"]) == 1


# ── Criterion 2 ──────────────────────────────────────────────────────────────
# When the cap or the privacy guard changed the agent or model before the task
# ran, result.route_note holds a short readable reason, e.g.
# 'opus cap reached -> sonnet' or 'deepseek not allowed for this project -> claude'.
# This is taken from payload._role_note or the router decision.


@pytest.mark.asyncio
async def test_c2_route_note_populated_when_cap_or_privacy_guard_changes_agent_or_model(api, monkeypatch):
    _fake_agents(
        monkeypatch,
        {
            "claude": _ok_agent("claude", "sonnet"),
            "codex": _ok_agent("codex", "gpt-5"),
            "agy": _ok_agent("agy", "gemini-2.5-pro"),
            "deepseek": _ok_agent("deepseek", "deepseek-chat"),
        },
    )

    # Case A: Opus cap reached for product_owner -> falls back to sonnet with route_note from _role_note
    spec = roles.load_policy()["roles"]["product_owner"]
    cap = spec["monthly_cost_cap_usd"]
    spent_task = await api.db.insert_task(
        Task(type=TaskType.agent_run, created_by="t", payload={"role": "product_owner"})
    )
    await api.db.update_task(
        spent_task.id,
        {
            "status": TaskStatus.done,
            "result": {
                "agent": "claude",
                "model": "opus",
                "usage": {"tokens_in": 10, "tokens_out": 10, "cost_usd": float(cap) + 1.0},
            },
        },
    )
    r_cap = await _queue_and_run(api, {"role": "product_owner", "prompt": "prioritize backlog"})
    assert isinstance(r_cap.get("route_note"), str) and len(r_cap["route_note"]) > 0
    assert "cap reached" in r_cap["route_note"].lower()

    # Case B: Privacy guard on role (deepseek not allowed for unlisted project or private sensitivity)
    r_priv_role = await _queue_and_run(
        api, {"role": "dev_backend", "project": "unlisted-proj", "prompt": "build endpoint"}
    )
    assert isinstance(r_priv_role.get("route_note"), str) and len(r_priv_role["route_note"]) > 0
    assert "deepseek" in r_priv_role["route_note"].lower() and "claude" in r_priv_role["route_note"].lower()

    # Case C: Router privacy decision when sensitivity="private" and agent="deepseek"
    r_router_priv = await _queue_and_run(
        api, {"agent": "deepseek", "sensitivity": "private", "prompt": "check private finances"}
    )
    assert r_router_priv["answered_by"] != "deepseek"
    assert isinstance(r_router_priv.get("route_note"), str) and len(r_router_priv["route_note"]) > 0


# ── Criterion 3 ──────────────────────────────────────────────────────────────
# Test in tests/test_failover.py: claude is forced to return a usage limit, so
# the task fails over to the next agent. The test asserts answered_by is that
# next agent, answered_model is its model, and
# failover[0] == {agent: 'claude', reason: 'limit', ...}.


def test_c3_claude_limit_fails_over_and_sets_answered_by_answered_model_and_failover_entry(monkeypatch):
    _fake_agents(
        monkeypatch,
        {
            "claude": LIMITED_CLAUDE,
            "codex": _ok_agent("codex", "gpt-5"),
            "agy": _ok_agent("agy", "gemini-2.5-pro"),
            "deepseek": _ok_agent("deepseek", "deepseek-chat"),
        },
    )
    r = asyncio.run(runner.run_agent(task_kind="plan", prompt="plan it"))
    assert r["ok"] is True
    assert r["answered_by"] == "codex"
    assert r["answered_model"] == "gpt-5"
    assert isinstance(r.get("failover"), list) and len(r["failover"]) >= 1
    assert r["failover"][0]["agent"] == "claude"
    assert r["failover"][0]["reason"] == "limit"


# ── Criterion 4 ──────────────────────────────────────────────────────────────
# Test: a task with no failover has answered_by equal to the queued agent and
# failover == [].


@pytest.mark.asyncio
async def test_c4_no_failover_has_answered_by_equal_to_queued_agent_and_empty_failover_list(api, monkeypatch):
    _fake_agents(
        monkeypatch,
        {
            "claude": _ok_agent("claude", "sonnet"),
            "codex": _ok_agent("codex", "gpt-5"),
            "agy": _ok_agent("agy", "gemini-2.5-pro"),
            "deepseek": _ok_agent("deepseek", "deepseek-chat"),
        },
    )
    for queued_agent in ("claude", "codex", "agy", "deepseek"):
        r = await _queue_and_run(api, {"agent": queued_agent, "prompt": "ping"})
        assert r["answered_by"] == queued_agent
        assert r["failover"] == []


# ── Criterion 5 ──────────────────────────────────────────────────────────────
# Old tasks that lack these fields still display: clients fall back to
# result.agent and then 'unknown' (covered in the S-3 and S-4 tests).


def test_c5_old_tasks_without_new_fields_fall_back_to_result_agent_then_unknown():
    from shared import tracking

    # Old result with only result.agent
    old_with_agent = {"ok": True, "agent": "claude", "response": "legacy answer"}
    assert tracking.from_result(old_with_agent).get("agent") == "claude"
    display_agent_1 = old_with_agent.get("answered_by") or old_with_agent.get("agent") or "unknown"
    assert display_agent_1 == "claude"

    # Old result lacking both answered_by and agent
    old_without_agent = {"ok": True, "response": "ancient answer"}
    assert tracking.from_result(old_without_agent).get("agent") is None
    display_agent_2 = old_without_agent.get("answered_by") or old_without_agent.get("agent") or "unknown"
    assert display_agent_2 == "unknown"
