"""GET /scrum/usage: cache-token totals, LLM usage coverage and the cost note (both back ends)."""
from __future__ import annotations

import pytest

from shared import tracking
from shared.models import Task, TaskStatus, TaskType
from tests.test_usage_columns import backend  # noqa: F401  (sqlite + rqlite fixture)


async def _add(db, ttype, result):
    t = await db.insert_task(Task(type=ttype, created_by="t"))
    await db.update_task(t.id, {"status": TaskStatus.done, "result": result})


async def _seed(db):
    if hasattr(db, "ensure_schema"):
        await db.ensure_schema()
    u = {"tokens_in": 10, "tokens_out": 5, "tokens_cache_read": 7, "tokens_cache_write": 2}
    await _add(db, TaskType.agent_run, {"ok": True, "agent": "claude", "usage": u})
    await _add(db, TaskType.agent_run, {"ok": True, "agent": "claude", "usage": u})
    await _add(db, TaskType.agent_run, {"ok": True, "agent": "claude"})
    await _add(db, TaskType.run_script, {"ok": True, "stdout": "hi"})


@pytest.mark.asyncio
async def test_coverage_totals_and_note(backend):  # noqa: F811
    db = backend[0]
    await _seed(db)
    rows = await db.usage_rows()
    cov = tracking.coverage(rows)
    assert cov["llm_tasks"] == 3 and cov["llm_tasks_with_usage"] == 2 and cov["percent"] == 66.7
    totals = tracking.summarise(rows, "all")[0]
    assert totals["tokens_cache_read"] == 14 and totals["tokens_cache_write"] == 4
    assert "not an invoice" in tracking.COST_NOTE


@pytest.mark.asyncio
async def test_endpoint_response(backend, monkeypatch):  # noqa: F811
    db = backend[0]
    await _seed(db)
    import orchestrator.main as main
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "_check_auth", lambda _k: None)
    out = await main.scrum_usage(group_by="agent", since=None, until=None, project=None, sprint=None,
                                 x_secret_key="")
    assert out["coverage"]["percent"] == 66.7 and out["coverage"]["llm_tasks"] == 3
    assert out["tasks"] == 4 and out["totals"]["tokens_cache_read"] == 14
    assert out["cost_note"] and "tokens_in" in out["totals"]


def test_percent_null_when_no_llm_tasks():
    assert tracking.coverage([{"agent": "none", "llm": 0}, {"llm": None, "agent": None}])["percent"] is None


def test_null_llm_uses_agent_and_old_rows_respect_since():
    rows = [{"llm": None, "agent": "claude", "tokens_in": 1, "created_at": "2026-11-01T00:00:00Z"},
            {"llm": None, "agent": "claude", "tokens_in": None, "created_at": "2026-10-01T00:00:00Z"}]
    assert tracking.coverage(rows)["llm_tasks"] == 2
    assert tracking.coverage(rows, "2026-10-01")["llm_tasks"] == 2
    assert tracking.coverage(rows, "2026-11-01")["llm_tasks"] == 1
