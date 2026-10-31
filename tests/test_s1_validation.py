"""Independent acceptance-criteria validation tests for story S-1:
Orchestrator serves one list of LLM choices and maps each choice onto agent/model/role.
Written strictly from the acceptance criteria and public interfaces.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from agents import claude_agent, roles
from shared.models import Task, TaskStatus, TaskType


@pytest.fixture
def api(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    import orchestrator.db as db_mod
    importlib.reload(db_mod)
    import orchestrator.main as m
    importlib.reload(m)
    return m


async def _get_choices(api):
    return await api.llm_choices(x_secret_key="test-secret")


async def _post_task(api, payload: dict):
    return await api.create_task(
        api.TaskCreate(type=TaskType.agent_run, payload=payload),
        x_secret_key="test-secret",
        x_client="",
    )


# ── Criterion 1 ──────────────────────────────────────────────────────────────
# GET /llm-choices returns an ordered list of {id, label, kind: auto|agent|role, payload, note}.
# The ids are auto, claude, claude-opus, codex, agy, deepseek, plus one role:<name> per role
# in config/roles.yaml (product_owner, scrum_master, analyst, dev_frontend, dev_backend, integrator, reviewer).
# Role entries are read from the file, not hard-coded, so adding a role to roles.yaml adds it to the response
# with no code change.

@pytest.mark.asyncio
async def test_ac1_llm_choices_order_schema_and_dynamic_roles(api, monkeypatch):
    choices = await _get_choices(api)
    assert isinstance(choices, list)
    for item in choices:
        assert set(item.keys()) == {"id", "label", "kind", "payload", "note"}
        assert item["kind"] in {"auto", "agent", "role"}

    # Check dynamic loading from roles.yaml first
    pol = roles.load_policy()
    pol_copy = {"roles": dict(pol["roles"]), **{k: v for k, v in pol.items() if k != "roles"}}
    pol_copy["roles"]["custom_dynamic_role"] = {"agent": "claude", "model": "sonnet", "best_for": "testing"}
    monkeypatch.setattr(roles, "load_policy", lambda: pol_copy)
    updated_choices = await _get_choices(api)
    assert "role:custom_dynamic_role" in [c["id"] for c in updated_choices]

    # Check ordered IDs from the criterion against the unmodified choices
    file_roles = list(pol["roles"].keys())
    expected_prefix = ["auto", "claude", "claude-opus", "codex", "agy", "deepseek"]
    expected_ids = expected_prefix + [f"role:{r}" for r in file_roles]
    assert [c["id"] for c in choices] == expected_ids


# ── Criterion 2 ──────────────────────────────────────────────────────────────
# The mapping is fixed and covered by tests: auto sends no agent, model or role, so the router uses
# default_class claude/sonnet. claude -> {agent: claude}; codex -> {agent: codex}; agy -> {agent: agy};
# deepseek -> {agent: deepseek}; role:X -> {role: X} only (no agent or model, so the role decides;
# explicit agent/model would override it); claude-opus -> {role: opus_chat}.

@pytest.mark.asyncio
async def test_ac2_choice_payload_mapping(api):
    choices = await _get_choices(api)
    by_id = {c["id"]: c["payload"] for c in choices}

    assert by_id["auto"] == {}
    assert by_id["claude"] == {"agent": "claude"}
    assert by_id["codex"] == {"agent": "codex"}
    assert by_id["agy"] == {"agent": "agy"}
    assert by_id["deepseek"] == {"agent": "deepseek"}

    for c in choices:
        if c["id"].startswith("role:"):
            role_name = c["id"].split(":", 1)[1]
            assert c["payload"] == {"role": role_name}
            assert "agent" not in c["payload"] and "model" not in c["payload"]

    assert "claude-opus" in by_id
    assert by_id["claude-opus"] == {"role": "opus_chat"}


# ── Criterion 3 ──────────────────────────────────────────────────────────────
# A new opus_chat role is added to config/roles.yaml with allow_opus: true, a monthly_cost_cap_usd,
# warn_at and cap_fallback {agent: claude, model: sonnet}. Opus is reached through the existing role grant.
# claude_agent.BLOCKED_MODEL_SUBSTRINGS is not loosened. A test shows that POST /tasks with
# payload.model=opus and no role is still refused by the agent.

@pytest.mark.asyncio
async def test_ac3_opus_chat_role_and_direct_opus_refused(api):
    assert "opus" in claude_agent.BLOCKED_MODEL_SUBSTRINGS

    t = await _post_task(api, {"agent": "claude", "model": "opus", "prompt": "hello"})
    assert not t.payload.get("_allow_opus")
    res = await claude_agent.run("hello", t.payload["model"], allow_opus=bool(t.payload.get("_allow_opus")))
    assert res["ok"] is False and "opus blocked" in res["error"].lower()

    pol_roles = roles.load_policy()["roles"]
    assert "opus_chat" in pol_roles
    oc = pol_roles["opus_chat"]
    assert oc.get("allow_opus") is True
    assert "monthly_cost_cap_usd" in oc and oc["monthly_cost_cap_usd"] > 0
    assert "warn_at" in oc and 0 < oc["warn_at"] < 1
    assert oc.get("cap_fallback") == {"agent": "claude", "model": "sonnet"}


# ── Criterion 4 ──────────────────────────────────────────────────────────────
# Test: when _month_opus_cost() is at or above the cap, a claude-opus task is stored with model sonnet
# and a _role_note saying the cap was reached. Between warn_at and the cap, the note is set and the model
# stays opus.

@pytest.mark.asyncio
async def test_ac4_claude_opus_cost_cap_and_warn_at(api, monkeypatch):
    pol_roles = roles.load_policy()["roles"]
    assert "opus_chat" in pol_roles, "opus_chat role missing from config/roles.yaml"
    spec = pol_roles["opus_chat"]
    cap = float(spec["monthly_cost_cap_usd"])
    warn_threshold = cap * float(spec["warn_at"])

    # Between warn_at and cap
    mid_cost = (warn_threshold + cap) / 2.0
    monkeypatch.setattr(api, "_month_opus_cost", lambda: mid_cost)
    t_warn = await _post_task(api, {"role": "opus_chat", "prompt": "test warn"})
    assert t_warn.payload.get("model") == "opus"
    assert t_warn.payload.get("_role_note")

    # At or above cap
    monkeypatch.setattr(api, "_month_opus_cost", lambda: cap)
    t_cap = await _post_task(api, {"role": "opus_chat", "prompt": "test cap"})
    assert t_cap.payload.get("model") == "sonnet"
    assert "cap reached" in t_cap.payload.get("_role_note", "").lower()


# ── Criterion 5 ──────────────────────────────────────────────────────────────
# Test: POST /tasks with role 'nonexistent' returns a 4xx whose body lists the valid role names,
# and no task is queued.

@pytest.mark.asyncio
async def test_ac5_post_tasks_nonexistent_role_returns_4xx_and_queues_nothing(api):
    valid_roles = list(roles.load_policy()["roles"].keys())
    with pytest.raises(HTTPException) as exc_info:
        await _post_task(api, {"role": "nonexistent", "prompt": "hello"})
    assert 400 <= exc_info.value.status_code < 500
    detail = str(exc_info.value.detail)
    for r in valid_roles:
        assert r in detail
    assert await api.db.list_tasks(limit=10) == []


# ── Criterion 6 ──────────────────────────────────────────────────────────────
# The response contains no keys, tokens or machine secrets; a test checks that every value
# is a short string or a dict of agent/model/role.

@pytest.mark.asyncio
async def test_ac6_response_values_are_short_strings_or_agent_model_role_dict(api):
    choices = await _get_choices(api)
    assert len(choices) > 0
    secret_key_names = ("secret", "token", "key", "password", "passwd")
    for item in choices:
        assert isinstance(item, dict)
        for k, v in item.items():
            assert not any(s in k.lower() for s in secret_key_names)
            if isinstance(v, str):
                assert len(v) <= 200
            elif isinstance(v, dict):
                assert set(v.keys()) <= {"agent", "model", "role"}
                for pk, pv in v.items():
                    assert isinstance(pv, str) and len(pv) <= 200
            else:
                pytest.fail(f"Unexpected value type {type(v)} for key {k}")

