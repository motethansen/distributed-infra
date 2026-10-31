"""Scrum roles: config/roles.yaml -> agent/model, the Opus allow-list and cap, the DeepSeek
project limit, and that a caller can never grant itself Opus."""
from __future__ import annotations

import stat

import pytest
from fastapi import HTTPException

from agents import roles
from shared import tracking
from shared.models import Task, TaskStatus, TaskType

POLICY = {
    "roles": {
        "product_owner": {"agent": "claude", "model": "opus", "allow_opus": True, "monthly_cost_cap_usd": 50,
                          "warn_at": 0.8, "cap_fallback": {"agent": "agy"}},
        "strict_po": {"agent": "claude", "model": "opus", "allow_opus": True, "monthly_cost_cap_usd": 10},
        "scrum_master": {"agent": "claude", "model": "sonnet"},
        "sneaky": {"agent": "claude", "model": "opus"},                      # opus without allow_opus
        "dev_backend": {"agent": "deepseek", "model": "deepseek-chat",
                        "private_fallback": {"agent": "claude", "model": "sonnet"}},
        "reviewer": {"agent": "codex"},
    },
    "deepseek_projects": ["Open-Thing"],
    "isolated_projects": ["Walled"],
}


# ── resolver ─────────────────────────────────────────────────────────────────

def test_plain_role_resolves_to_its_agent_and_model():
    r = roles.resolve("scrum_master", policy=POLICY)
    assert (r["agent"], r["model"], r["allow_opus"]) == ("claude", "sonnet", False)
    assert roles.resolve("Reviewer", policy=POLICY)["agent"] == "codex"       # case-insensitive


def test_unknown_role_is_refused():
    with pytest.raises(roles.RoleError, match="unknown role"):
        roles.resolve("ceo", policy=POLICY)


def test_opus_needs_allow_opus_on_the_role():
    assert roles.resolve("product_owner", policy=POLICY)["allow_opus"] is True
    with pytest.raises(roles.RoleError, match="may not use Opus"):
        roles.resolve("sneaky", policy=POLICY)


def test_opus_cap_stops_the_role_at_the_limit_not_after():
    assert roles.resolve("strict_po", month_opus_cost=9.99, policy=POLICY)["allow_opus"]
    with pytest.raises(roles.RoleError, match="monthly Opus cap reached"):     # no fallback: refuse
        roles.resolve("strict_po", month_opus_cost=10.0, policy=POLICY)


def test_spent_cap_runs_the_role_on_its_fallback_instead_of_stopping():
    r = roles.resolve("product_owner", month_opus_cost=50.0, policy=POLICY)
    assert (r["agent"], r["allow_opus"]) == ("agy", False) and "opus cap reached" in r["note"]
    ok = roles.resolve("product_owner", month_opus_cost=49.0, policy=POLICY)
    assert ok["agent"] == "claude" and ok["allow_opus"] and "98%" in ok["note"]          # near the cap: warned
    assert roles.resolve("product_owner", month_opus_cost=10.0, policy=POLICY)["note"] == ""


def test_isolated_project_needs_explicit_approval():
    with pytest.raises(roles.RoleError, match="isolated"):
        roles.resolve("scrum_master", project="walled", policy=POLICY)
    assert roles.resolve("scrum_master", project="Walled", approved=True, policy=POLICY)["agent"] == "claude"


def test_a_prompt_that_carries_a_credential_never_reaches_deepseek():
    for leak in ("use sk_live_abcdefgh12345678 here", "password = hunter2hunter2", "-----BEGIN RSA PRIVATE KEY-----",
                 "AKIAABCDEFGHIJKLMNOP"):
        r = roles.resolve("dev_backend", project="open-thing", prompt=leak, policy=POLICY)
        assert r["agent"] == "claude" and "credential" in r["note"], leak
    assert roles.resolve("dev_backend", project="open-thing", prompt="write a FastAPI route for /health",
                         policy=POLICY)["agent"] == "deepseek"


def test_deepseek_only_for_listed_projects_and_never_for_sensitive_work():
    assert roles.resolve("dev_backend", project="open-thing", policy=POLICY)["agent"] == "deepseek"
    for project, sens in (("budgetapp", None), (None, None), ("open-thing", "private")):
        r = roles.resolve("dev_backend", project=project, sensitivity=sens, policy=POLICY)
        assert (r["agent"], r["model"]) == ("claude", "sonnet") and "deepseek not used" in r["note"]


def test_shipped_policy_is_loadable_and_safe_by_default():
    p = roles.load_policy()
    assert set(roles.role_names()) >= {"product_owner", "scrum_master", "analyst", "dev_frontend", "dev_backend", "reviewer"}
    assert "urbanlife" not in p["deepseek_projects"] and "urbanlife" in p["isolated_projects"]
    with pytest.raises(roles.RoleError, match="isolated"):
        roles.resolve("dev_backend", project="urbanlife")
    opus_roles = [n for n, s in p["roles"].items() if roles.is_opus(s.get("model"))]
    assert opus_roles == ["product_owner"] and p["roles"]["product_owner"]["allow_opus"] is True
    assert roles.resolve("dev_backend", project="budgetapp")["agent"] == "deepseek"
    assert roles.resolve("dev_backend", project="budgetapp", sensitivity="private")["agent"] == "claude"


# ── Opus gate in the agent ───────────────────────────────────────────────────

def _fake_cli(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho '{\"result\":\"ok\",\"is_error\":false}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return str(fake)


@pytest.mark.asyncio
async def test_claude_agent_refuses_opus_unless_granted(tmp_path, monkeypatch):
    from agents import claude_agent
    monkeypatch.setattr(claude_agent, "_find_cli", lambda: _fake_cli(tmp_path))
    blocked = await claude_agent.run("hi", "opus")
    assert not blocked["ok"] and "opus blocked" in blocked["error"]
    assert (await claude_agent.run("hi", "opus-5-5", allow_opus=True))["ok"]


@pytest.mark.asyncio
async def test_handler_takes_the_grant_only_from_the_orchestrator_stamp(monkeypatch):
    from worker.handlers import agent_run
    import agents.runner as runner
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return {"ok": True, "agent": "claude"}

    monkeypatch.setattr(runner, "run_agent", fake)
    await agent_run.handle_agent_run(Task(type=TaskType.agent_run, created_by="t", payload={"agent": "claude", "prompt": "x", "_allow_opus": True}))
    assert seen["allow_opus"] is True
    await agent_run.handle_agent_run(Task(type=TaskType.agent_run, created_by="t", payload={"agent": "claude", "prompt": "x", "allow_opus": True}))
    assert seen["allow_opus"] is False                                         # un-prefixed key is not the stamp


# ── queueing a task with a role ──────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "k")
    import orchestrator.db as db_mod
    importlib.reload(db_mod)
    import orchestrator.main as m
    importlib.reload(m)
    monkeypatch.setattr(roles, "load_policy", lambda: POLICY)
    return m


async def _queue(api, payload, client=""):
    body = api.TaskCreate(type=TaskType.agent_run, payload=payload)
    return await api.create_task(body, x_secret_key="k", x_client=client)


@pytest.mark.asyncio
async def test_role_fills_agent_model_and_is_recorded_on_the_row(api):
    t = await _queue(api, {"role": "Scrum_Master", "prompt": "plan", "project": "budgetapp", "sprint": "S5", "story": "BA-1"})
    assert (t.payload["agent"], t.payload["model"], t.payload["role"]) == ("claude", "sonnet", "scrum_master")
    row = await api.db.get_task(t.id)
    assert row.role == "scrum_master" and row.sprint == "S5"
    assert "_allow_opus" not in t.payload


@pytest.mark.asyncio
async def test_explicit_agent_beats_the_role(api):
    t = await _queue(api, {"role": "scrum_master", "agent": "agy", "prompt": "x"})
    assert t.payload["agent"] == "agy" and "model" not in t.payload


@pytest.mark.asyncio
async def test_caller_cannot_grant_itself_opus(api):
    t = await _queue(api, {"agent": "claude", "model": "opus", "_allow_opus": True, "prompt": "x"})
    assert "_allow_opus" not in t.payload
    t = await _queue(api, {"role": "scrum_master", "_allow_opus": True, "prompt": "x"})
    assert "_allow_opus" not in t.payload
    # a role without Opus rights does not gain them by pinning the model itself
    t = await _queue(api, {"role": "scrum_master", "model": "opus", "prompt": "x"})
    assert t.payload["model"] == "opus" and "_allow_opus" not in t.payload
    with pytest.raises(HTTPException) as e:
        await _queue(api, {"role": "sneaky", "prompt": "x"})
    assert e.value.status_code == 422


@pytest.mark.asyncio
async def test_product_owner_gets_the_grant_until_the_cap_is_spent(api):
    t = await _queue(api, {"role": "product_owner", "prompt": "stories"})
    assert t.payload["model"] == "opus" and t.payload["_allow_opus"] is True
    spent = await api.db.insert_task(Task(type=TaskType.agent_run, created_by="t", payload={"role": "product_owner"}))
    await api.db.update_task(spent.id, {"status": TaskStatus.done, "result": {
        "agent": "claude", "model": "opus", "usage": {"tokens_in": 1, "tokens_out": 1, "cost_usd": 50.5}}})
    t = await _queue(api, {"role": "product_owner", "prompt": "more stories"})            # cap spent -> fallback
    assert t.payload["agent"] == "agy" and "_allow_opus" not in t.payload and "cap reached" in t.payload["_role_note"]
    with pytest.raises(HTTPException) as e:
        await _queue(api, {"role": "strict_po", "prompt": "more"})
    assert e.value.status_code == 422 and "cap reached" in e.value.detail
    # other roles are unaffected by the Opus cap
    assert (await _queue(api, {"role": "scrum_master", "prompt": "x"})).payload["agent"] == "claude"


@pytest.mark.asyncio
async def test_unknown_role_is_422_and_dev_backend_defaults_to_claude_for_unlisted_projects(api):
    with pytest.raises(HTTPException) as e:
        await _queue(api, {"role": "ceo", "prompt": "x"})
    assert e.value.status_code == 422
    t = await _queue(api, {"role": "dev_backend", "project": "budgetapp", "prompt": "route"})
    assert t.payload["agent"] == "claude" and "deepseek not used" in t.payload["_role_note"]
    t = await _queue(api, {"role": "dev_backend", "project": "open-thing", "prompt": "route"})
    assert t.payload["agent"] == "deepseek"
    with pytest.raises(HTTPException) as e:                                              # isolated project
        await _queue(api, {"role": "scrum_master", "project": "walled", "prompt": "x"})
    assert e.value.status_code == 422
    assert (await _queue(api, {"role": "scrum_master", "project": "walled", "approved": True, "prompt": "x"})).payload["agent"] == "claude"


def test_role_is_a_summary_group():
    rows = [{"status": "done", "role": "reviewer", "agent": "codex"}, {"status": "done", "role": None}]
    assert {g["key"] for g in tracking.summarise(rows, "role")} == {"reviewer", "(untagged)"}


# ── isolated projects: every task, not only role tasks ───────────────────────

def test_isolated_violation_covers_project_tag_and_cwd_whatever_the_role_or_client():
    pol = {"isolated_projects": ["Walled"]}
    assert "isolated" in roles.isolated_violation({"project": "walled", "agent": "claude", "prompt": "x"}, pol)
    assert "isolated" in roles.isolated_violation({"cwd": "~/Projects/github/Vizneo/walled_works", "agent": "codex"}, pol)
    assert roles.isolated_violation({"project": "walled", "approved": True}, pol) is None
    assert "isolated" in roles.isolated_violation({"project": "walled", "approved": "yes"}, pol)      # only a real True counts
    assert roles.isolated_violation({"project": "other", "cwd": "~/Projects/other"}, pol) is None
    assert roles.isolated_violation({"agent": "claude"}, pol) is None and roles.isolated_violation({}, {}) is None


@pytest.mark.asyncio
async def test_a_task_with_no_role_for_an_isolated_project_is_refused_unless_approved(api):
    for payload in ({"agent": "claude", "project": "walled", "prompt": "x"},
                    {"agent": "deepseek", "cwd": "~/Projects/walled_app", "prompt": "x"}):
        with pytest.raises(HTTPException) as e:
            await _queue(api, dict(payload))
        assert e.value.status_code == 422 and "isolated" in e.value.detail
    assert await api.db.list_tasks(limit=10) == []                                        # nothing was queued
    t = await _queue(api, {"agent": "claude", "project": "walled", "approved": True, "prompt": "x"})
    assert t.payload["agent"] == "claude"
