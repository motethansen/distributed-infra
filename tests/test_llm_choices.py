"""GET /llm-choices: one list, a fixed choice -> payload mapping, Opus only via the product_owner role."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from agents import roles
from shared.models import TaskType

IDS = ["auto", "claude", "codex", "agy", "deepseek"]
ROLE_NAMES = ["product_owner", "scrum_master", "analyst", "dev_frontend", "dev_backend", "integrator", "reviewer"]


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
    return m


async def _choices(api):
    return await api.llm_choices(x_secret_key="k")


async def _queue(api, payload):
    return await api.create_task(api.TaskCreate(type=TaskType.agent_run, payload=payload), x_secret_key="k", x_client="")


@pytest.mark.asyncio
async def test_list_is_ordered_and_roles_come_from_the_file(api, monkeypatch):
    got = await _choices(api)
    assert [c["id"] for c in got] == IDS + [f"role:{n}" for n in ROLE_NAMES]
    assert all(set(c) == {"id", "label", "kind", "payload", "note"} for c in got)
    assert {c["kind"] for c in got} == {"auto", "agent", "role"}
    # a role added to the file shows up with no code change
    pol = roles.load_policy()
    pol["roles"]["newcomer"] = {"agent": "claude", "model": "haiku", "best_for": "run tests"}
    monkeypatch.setattr(api.scrum_roles, "load_policy", lambda: pol)        # restored by the fixture: no leak to later tests
    assert "role:newcomer" in [c["id"] for c in await _choices(api)]


@pytest.mark.asyncio
async def test_mapping_is_fixed(api):
    by = {c["id"]: c["payload"] for c in await _choices(api)}
    assert by["auto"] == {}
    assert by["claude"] == {"agent": "claude"} and by["codex"] == {"agent": "codex"}
    assert by["agy"] == {"agent": "agy"} and by["deepseek"] == {"agent": "deepseek"}
    assert "claude-opus" not in by and not any("opus" in k for k in by)        # Opus is not a chat choice
    assert by["role:product_owner"] == {"role": "product_owner"}
    for n in ROLE_NAMES:
        assert by[f"role:{n}"] == {"role": n}


@pytest.mark.asyncio
async def test_response_has_only_short_strings_and_agent_model_role(api):
    for c in await _choices(api):
        for k in ("id", "label", "kind", "note"):
            assert isinstance(c[k], str) and len(c[k]) <= 200
        assert set(c["payload"]) <= {"agent", "model", "role"}
        assert all(isinstance(v, str) and len(v) <= 200 for v in c["payload"].values())


def test_product_owner_is_the_only_role_that_may_use_opus():
    pol = roles.load_policy()["roles"]
    assert [n for n, sp in pol.items() if sp.get("allow_opus")] == ["product_owner"]
    assert "opus_chat" not in pol


@pytest.mark.asyncio
async def test_opus_without_role_is_still_refused_by_the_agent(api, tmp_path, monkeypatch):
    from agents import claude_agent
    assert claude_agent.BLOCKED_MODEL_SUBSTRINGS == ("opus",)
    t = await _queue(api, {"agent": "claude", "model": "opus", "prompt": "x"})
    assert "_allow_opus" not in t.payload
    r = await claude_agent.run("hi", t.payload["model"], allow_opus=bool(t.payload.get("_allow_opus")))
    assert not r["ok"] and "opus blocked" in r["error"]


async def _spend(api, usd):
    from shared.models import Task, TaskStatus
    t = await api.db.insert_task(Task(type=TaskType.agent_run, created_by="t", payload={"role": "product_owner"}))
    await api.db.update_task(t.id, {"status": TaskStatus.done, "result": {
        "agent": "claude", "model": "opus", "usage": {"tokens_in": 1, "tokens_out": 1, "cost_usd": usd}}})


@pytest.mark.asyncio
async def test_product_owner_opus_cap_boundary_and_warning(api):
    spec = roles.load_policy()["roles"]["product_owner"]
    cap, warn = spec["monthly_cost_cap_usd"], spec["warn_at"]
    po = {"role": "product_owner", "prompt": "hi"}
    t = await _queue(api, dict(po))
    assert t.payload["model"] == "opus" and t.payload["_allow_opus"] is True and "_role_note" not in t.payload
    await _spend(api, cap * warn + 0.01)                                   # between warn_at and the cap: still Opus, flagged
    t = await _queue(api, dict(po))
    assert t.payload["model"] == "opus" and t.payload["_allow_opus"] is True and "_role_note" in t.payload
    await _spend(api, cap - (cap * warn + 0.01) - 0.01)                    # total is now cap - 0.01: still under
    t = await _queue(api, dict(po))
    assert t.payload["model"] == "opus" and t.payload["_allow_opus"] is True
    await _spend(api, 0.01)                                                # total is EXACTLY the cap: falls back
    t = await _queue(api, dict(po))
    assert t.payload["agent"] == "claude" and t.payload["model"] == "sonnet" and "_allow_opus" not in t.payload
    assert "cap reached" in t.payload["_role_note"]


@pytest.mark.asyncio
async def test_unknown_role_is_4xx_lists_valid_roles_and_queues_nothing(api):
    with pytest.raises(HTTPException) as e:
        await _queue(api, {"role": "nonexistent", "prompt": "x"})
    assert 400 <= e.value.status_code < 500
    assert all(n in e.value.detail for n in ROLE_NAMES)
    assert await api.db.list_tasks(limit=10) == []


@pytest.mark.asyncio
async def test_role_name_is_trimmed_and_case_insensitive(api):
    t = await _queue(api, {"role": " Dev_Backend ", "prompt": "x"})
    assert t.payload["role"] == "dev_backend"
    assert len(await api.db.list_tasks(limit=10)) == 1


@pytest.mark.asyncio
async def test_no_secret_looking_keys_anywhere(api):
    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    bad = ("token", "key", "secret")
    assert not [k for k in keys(await _choices(api)) if any(b in k.lower() for b in bad)]
