"""S-1 acceptance: LLM usage recorded per task, DeepSeek cost from the price table, coverage endpoint, both back ends."""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3

import pytest

from agents import deepseek_agent as ds
from agents.claude_agent import parse_json_output
from shared.models import Task, TaskStatus, TaskType

CLAUDE_OK = {"type": "result", "subtype": "success", "is_error": False, "result": "all done",
             "total_cost_usd": 0.1234, "modelUsage": {"claude-sonnet-5-5": {"costUSD": 0.1234}},
             "usage": {"input_tokens": 21, "cache_creation_input_tokens": 900,
                       "cache_read_input_tokens": 4300, "output_tokens": 77}}
CLAUDE_ERR = {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "ran out of turns",
              "total_cost_usd": 0.02, "model": "claude-sonnet-5-5",
              "usage": {"input_tokens": 55, "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 20, "output_tokens": 8}}


@pytest.fixture(params=["sqlite", "rqlite"])
def db(request, tmp_path, monkeypatch):
    path = tmp_path / "q.db"
    if request.param == "sqlite":
        monkeypatch.setenv("QUEUE_DB_PATH", str(path))
        import orchestrator.db as mod
        importlib.reload(mod)
        return mod
    import orchestrator.db_rqlite as mod
    conn = sqlite3.connect(path)

    async def fake_post(_p, statements, params=None):
        await asyncio.sleep(0)
        out = []
        for sql, *args in statements:
            cur = conn.execute(sql, args)
            if cur.description:
                out.append({"columns": [d[0] for d in cur.description], "values": [list(x) for x in cur.fetchall()]})
            else:
                out.append({"last_insert_id": cur.lastrowid, "rows_affected": cur.rowcount})
        conn.commit()
        return out

    monkeypatch.setattr(mod, "_post", fake_post)
    return mod


async def _finish(db, ttype, status, result):
    if hasattr(db, "ensure_schema"):
        await db.ensure_schema()
    t = await db.insert_task(Task(type=ttype, created_by="t"))
    await db.update_task(t.id, {"status": status, "result": result})
    return await db.get_task(t.id)


def _result_from_cli(raw: dict) -> dict:
    text, usage, is_err = parse_json_output(json.dumps(raw))
    return {"ok": not is_err, "response": text, "agent": "claude", "model": usage["model"],
            "usage": {k: v for k, v in usage.items() if k not in ("agent", "model")}}


# --- 1 ---
def test_c1_parse_json_output_every_field():
    text, u, is_err = parse_json_output(json.dumps(CLAUDE_OK))
    assert text == "all done" and is_err is False
    assert u["agent"] == "claude" and u["model"] == "claude-sonnet-5-5"
    assert (u["tokens_in"], u["tokens_out"]) == (21, 77)
    assert (u["tokens_cache_read"], u["tokens_cache_write"]) == (4300, 900)
    assert u["cost_usd"] == pytest.approx(0.1234)


@pytest.mark.asyncio
async def test_c1_task_row_has_all_columns(db):
    t = await _finish(db, TaskType.agent_run, TaskStatus.done, _result_from_cli(CLAUDE_OK))
    assert t.agent == "claude" and t.model == "claude-sonnet-5-5"
    assert (t.tokens_in, t.tokens_out) == (21, 77)
    assert (t.tokens_cache_read, t.tokens_cache_write) == (4300, 900)
    assert t.cost_usd == pytest.approx(0.1234)


# --- 2 ---
@pytest.mark.asyncio
async def test_c2_failed_row_keeps_usage(db):
    _, u, is_err = parse_json_output(json.dumps(CLAUDE_ERR))
    assert is_err is True and u["tokens_in"] > 0
    t = await _finish(db, TaskType.agent_run, TaskStatus.failed, _result_from_cli(CLAUDE_ERR))
    assert t.status == TaskStatus.failed
    assert t.tokens_in == 55 and t.tokens_out == 8 and t.cost_usd == pytest.approx(0.02)


# --- 3 ---
class _Resp:
    def __init__(self, content, usage):
        self._d = {"choices": [{"message": {"content": content}}], "usage": usage}

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def test_c3_deepseek_cost_from_config_table_and_summed(monkeypatch, tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("deepseek:\n  deepseek-chat: {input_fresh: 3.0, input_cache_hit: 0.3, output: 6.0, verified: true}\n")
    monkeypatch.setattr(ds, "PRICES_PATH", f)
    ds._load_prices.cache_clear()
    monkeypatch.setenv("DEEPSEEK_API", "k")
    import httpx
    q = [_Resp("<function_calls><invoke name=\"bash\"></invoke></function_calls>",
               {"prompt_tokens": 1_000_000, "completion_tokens": 0}),
         _Resp("final", {"prompt_tokens": 0, "completion_tokens": 1_000_000})]

    class C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return q.pop(0)

    monkeypatch.setattr(httpx, "AsyncClient", C)
    out = asyncio.run(ds.run("hi"))
    ds._load_prices.cache_clear()
    assert out["ok"] is True
    assert out["usage"]["cost_usd"] == pytest.approx(3.0 + 6.0)       # 1st call 3.0 + retry 6.0
    assert out["usage"]["tokens_in"] == 1_000_000 and out["usage"]["tokens_out"] == 1_000_000


def test_c3_no_hardcoded_price_in_agent():
    import inspect
    src = inspect.getsource(ds)
    assert "llm_prices" in src


# --- 4 ---
@pytest.mark.asyncio
async def test_c4_non_llm_is_none_and_not_llm(db):
    t = await _finish(db, TaskType.run_script, TaskStatus.done, {"ok": True, "stdout": "x"})
    assert t.agent == "none" and t.llm is False


# --- 5 / 6 ---
@pytest.mark.asyncio
async def test_c5_scrum_usage_coverage(db, monkeypatch):
    u = {"tokens_in": 10, "tokens_out": 5}
    await _finish(db, TaskType.agent_run, TaskStatus.done, {"ok": True, "agent": "claude", "usage": u})
    await _finish(db, TaskType.agent_run, TaskStatus.done, {"ok": True, "agent": "claude", "usage": u})
    await _finish(db, TaskType.agent_run, TaskStatus.done, {"ok": True, "agent": "claude"})
    await _finish(db, TaskType.run_script, TaskStatus.done, {"ok": True, "stdout": "x"})
    import orchestrator.main as main
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "_check_auth", lambda _k: None)
    out = await main.scrum_usage(group_by="agent", since=None, until=None, project=None, sprint=None, x_secret_key="")
    cov = out["coverage"]
    assert cov["llm_tasks"] == 3 and cov["llm_tasks_with_usage"] == 2 and cov["percent"] == 66.7
