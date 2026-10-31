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


async def _record_opus_spend(api, cost_usd: float):
    t = await api.db.insert_task(
        Task(type=TaskType.agent_run, created_by="test", payload={"role": "product_owner"})
    )
    await api.db.update_task(
        t.id,
        {
            "status": TaskStatus.done,
            "result": {
                "agent": "claude",
                "model": "opus",
                "usage": {"tokens_in": 100, "tokens_out": 100, "cost_usd": cost_usd},
            },
        },
    )


# ── Criterion 1 ──────────────────────────────────────────────────────────────
# GET /llm-choices returns an ordered list of {id, label, kind: auto|agent|role, payload, note}.
# The ids are auto, claude, codex, agy, deepseek, then one role:<name> per role in config/roles.yaml.
# Role entries are read from the file, not hard-coded, so adding a role to roles.yaml adds it with no code change.

@pytest.mark.asyncio
async def test_ac1_llm_choices_order_schema_and_dynamic_roles(api, monkeypatch):
    choices = await _get_choices(api)
    assert isinstance(choices, list)
    for item in choices:
        assert set(item.keys()) == {"id", "label", "kind", "payload", "note"}
        assert item["kind"] in {"auto", "agent", "role"}

    pol = roles.load_policy()
    file_roles = list(pol["roles"].keys())
    expected_prefix = ["auto", "claude", "codex", "agy", "deepseek"]
    expected_ids = expected_prefix + [f"role:{r}" for r in file_roles]
    assert [c["id"] for c in choices] == expected_ids

    # Adding a role to roles.yaml adds it with no code change
    pol_copy = {"roles": dict(pol["roles"]), **{k: v for k, v in pol.items() if k != "roles"}}
    pol_copy["roles"]["custom_dynamic_role"] = {"agent": "claude", "model": "sonnet", "best_for": "testing"}
    monkeypatch.setattr(roles, "load_policy", lambda: pol_copy)
    updated_choices = await _get_choices(api)
    assert [c["id"] for c in updated_choices] == expected_ids + ["role:custom_dynamic_role"]


# ── Criterion 2 ──────────────────────────────────────────────────────────────
# The mapping is fixed: auto sends no agent, model or role; claude/codex/agy/deepseek send {agent: <id>};
# role:X sends {role: X} only. Opus is not a chat choice: no choice id contains 'opus'.

@pytest.mark.asyncio
async def test_ac2_choice_payload_mapping_and_no_opus_choice(api):
    choices = await _get_choices(api)
    by_id = {c["id"]: c["payload"] for c in choices}

    assert by_id["auto"] == {}
    assert not any(k in by_id["auto"] for k in ("agent", "model", "role"))

    for agent_id in ("claude", "codex", "agy", "deepseek"):
        assert by_id[agent_id] == {"agent": agent_id}

    for c in choices:
        assert "opus" not in c["id"].lower()
        if c["id"].startswith("role:"):
            role_name = c["id"].split(":", 1)[1]
            assert c["payload"] == {"role": role_name}
            assert "agent" not in c["payload"] and "model" not in c["payload"]


# ── Criterion 3 ──────────────────────────────────────────────────────────────
# Opus is only for the Product Owner: product_owner holds the only allow_opus grant in config/roles.yaml,
# and a task with model opus and no role is still refused by the agent (BLOCKED_MODEL_SUBSTRINGS is not loosened).

@pytest.mark.asyncio
async def test_ac3_opus_only_for_product_owner_and_direct_opus_refused(api):
    pol_roles = roles.load_policy()["roles"]
    allow_opus_roles = [name for name, spec in pol_roles.items() if spec.get("allow_opus")]
    assert allow_opus_roles == ["product_owner"]

    assert "opus" in claude_agent.BLOCKED_MODEL_SUBSTRINGS

    t = await _post_task(api, {"agent": "claude", "model": "opus", "prompt": "hello"})
    assert not t.payload.get("_allow_opus")
    res = await claude_agent.run("hello", t.payload["model"], allow_opus=bool(t.payload.get("_allow_opus")))
    assert res["ok"] is False and "opus blocked" in res["error"].lower()


# ── Criterion 4 ──────────────────────────────────────────────────────────────
# product_owner's monthly cost cap: below warn_at the task runs on Opus; between warn_at and the cap it
# still runs on Opus with a _role_note; at exactly the cap (and above) it is stored on sonnet with a note
# saying the cap was reached.

@pytest.mark.asyncio
async def test_ac4_product_owner_cost_cap_stages(api):
    spec = roles.load_policy()["roles"]["product_owner"]
    cap = float(spec["monthly_cost_cap_usd"])
    warn_threshold = cap * float(spec["warn_at"])

    # Below warn_at: runs on Opus without _role_note
    t_below = await _post_task(api, {"role": "product_owner", "prompt": "below warn"})
    assert t_below.payload.get("model") == "opus"
    assert t_below.payload.get("_allow_opus") is True
    assert "_role_note" not in t_below.payload

    # Between warn_at and cap: still runs on Opus with a _role_note
    await _record_opus_spend(api, warn_threshold + 0.01)
    t_warn = await _post_task(api, {"role": "product_owner", "prompt": "between warn and cap"})
    assert t_warn.payload.get("model") == "opus"
    assert t_warn.payload.get("_allow_opus") is True
    assert t_warn.payload.get("_role_note")

    # At exactly the cap: stored on sonnet with a note saying the cap was reached
    await _record_opus_spend(api, cap - (warn_threshold + 0.01))
    t_exact = await _post_task(api, {"role": "product_owner", "prompt": "at exact cap"})
    assert t_exact.payload.get("model") == "sonnet"
    assert not t_exact.payload.get("_allow_opus")
    assert "cap reached" in t_exact.payload.get("_role_note", "").lower()

    # Above the cap: also stored on sonnet with cap reached note
    await _record_opus_spend(api, 5.0)
    t_above = await _post_task(api, {"role": "product_owner", "prompt": "above cap"})
    assert t_above.payload.get("model") == "sonnet"
    assert not t_above.payload.get("_allow_opus")
    assert "cap reached" in t_above.payload.get("_role_note", "").lower()


# ── Criterion 5 ──────────────────────────────────────────────────────────────
# POST /tasks with an unknown role returns a 4xx whose body lists the valid role names, and no task is queued.

@pytest.mark.asyncio
async def test_ac5_post_tasks_unknown_role_returns_4xx_and_queues_nothing(api):
    valid_roles = list(roles.load_policy()["roles"].keys())
    with pytest.raises(HTTPException) as exc_info:
        await _post_task(api, {"role": "nonexistent_role", "prompt": "hello"})
    assert 400 <= exc_info.value.status_code < 500
    detail = str(exc_info.value.detail)
    for r in valid_roles:
        assert r in detail
    assert await api.db.list_tasks(limit=10) == []


# ── Criterion 6 ──────────────────────────────────────────────────────────────
# The /llm-choices response contains no keys, tokens or secrets: every value is a short string or
# a dict of agent/model/role.

@pytest.mark.asyncio
async def test_ac6_response_contains_no_secrets_and_only_short_strings_or_agent_model_role_dict(api):
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
                    assert not any(s in pk.lower() for s in secret_key_names)
                    assert isinstance(pv, str) and len(pv) <= 200
            else:
                pytest.fail(f"Unexpected value type {type(v)} for key {k}")


# ── Criterion 7 ──────────────────────────────────────────────────────────────
# A task for an isolated project (for example urbanlife) is refused without payload.approved: true
# whether or not it names a role, including a direct agent choice or a cwd inside that project.

@pytest.mark.asyncio
async def test_ac7_isolated_project_refused_without_approval_across_role_agent_and_cwd(api):
    unapproved_payloads = [
        {"role": "scrum_master", "project": "urbanlife", "prompt": "plan"},
        {"agent": "claude", "project": "urbanlife", "prompt": "direct agent"},
        {"agent": "codex", "cwd": "~/Projects/urbanlife/backend", "prompt": "cwd inside project"},
        {"cwd": "/Users/dev/urbanlife", "prompt": "auto with cwd"},
    ]
    for p in unapproved_payloads:
        with pytest.raises(HTTPException) as exc_info:
            await _post_task(api, dict(p))
        assert 400 <= exc_info.value.status_code < 500
        assert "isolated" in str(exc_info.value.detail).lower()

    assert await api.db.list_tasks(limit=10) == []

    # With payload.approved: true, tasks are accepted
    t_role = await _post_task(api, {"role": "scrum_master", "project": "urbanlife", "approved": True, "prompt": "ok"})
    assert t_role.payload["role"] == "scrum_master"
    t_agent = await _post_task(api, {"agent": "claude", "cwd": "~/Projects/urbanlife", "approved": True, "prompt": "ok"})
    assert t_agent.payload["agent"] == "claude"
