"""Usage columns (cache tokens, llm flag) on both queue back ends: additive migration of an
old-schema DB, and a finish/read round trip. rqlite runs on the fake HTTP layer over sqlite."""
from __future__ import annotations

import asyncio
import importlib
import sqlite3

import pytest

from shared.models import Task, TaskType
from shared import tracking

NEW = ("tokens_cache_read", "tokens_cache_write", "llm")
OLD_DDL = """CREATE TABLE tasks (id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 5, payload TEXT NOT NULL DEFAULT '{}', created_by TEXT NOT NULL,
    assigned_to TEXT, result TEXT, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    agent TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, tokens_total INTEGER, cost_usd REAL)"""
RESULT = {"agent": "deepseek", "model": "deepseek-chat", "llm": True,
          "usage": {"tokens_in": 100, "tokens_out": 20, "cost_usd": 0.0123,
                    "tokens_cache_read": 60, "tokens_cache_write": 7}}


@pytest.fixture(params=["sqlite", "rqlite"])
def backend(request, tmp_path, monkeypatch):
    path = tmp_path / "q.db"
    if request.param == "sqlite":
        monkeypatch.setenv("QUEUE_DB_PATH", str(path))
        import orchestrator.db as mod
        importlib.reload(mod)
        return mod, path, None
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
    return mod, path, conn


def _cols(path):
    c = sqlite3.connect(path)
    try:
        return {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    finally:
        c.close()


def _legacy(backend):
    mod, path, conn = backend
    c = conn or sqlite3.connect(path)
    c.execute(OLD_DDL)
    c.execute("INSERT INTO tasks (id,type,status,created_by,created_at,updated_at,tokens_in) "
              "VALUES ('old','run_script','done','m','2026-01-01T00:00:00','2026-01-01T00:00:00',5)")
    c.commit()
    if not conn:
        c.close()


async def _touch(mod):
    if hasattr(mod, "ensure_schema"):
        await mod.ensure_schema()
    else:
        await mod.get_task("nope")


def test_usage_cols_list_has_new_fields(backend):
    mod = backend[0]
    cols = mod._USAGE_COLS.split(",")
    assert all(c in cols for c in NEW) and len(cols) == len(set(cols))


@pytest.mark.asyncio
async def test_migration_adds_columns_to_old_schema_and_is_idempotent(backend):
    mod, path, _ = backend
    _legacy(backend)
    assert not set(NEW) & _cols(path)
    await _touch(mod)
    await _touch(mod)
    assert set(NEW) <= _cols(path)
    old = await mod.get_task("old")
    assert old.tokens_in == 5 and old.tokens_cache_read is None and old.llm is None


@pytest.mark.asyncio
async def test_round_trip_every_usage_field(backend):
    mod = backend[0]
    _legacy(backend)
    await _touch(mod)
    t = await mod.insert_task(Task(type=TaskType.run_script, created_by="m"))
    got = await mod.update_task(t.id, {"status": "done", "result": RESULT})
    for task in (got, await mod.get_task(t.id)):
        assert (task.agent, task.model) == ("deepseek", "deepseek-chat")
        assert (task.tokens_in, task.tokens_out, task.tokens_total) == (100, 20, 120)
        assert task.cost_usd == pytest.approx(0.0123)
        assert (task.tokens_cache_read, task.tokens_cache_write) == (60, 7)
        assert task.llm is True
    row = next(r for r in await mod.usage_rows() if r["id"] == t.id)
    assert row["tokens_cache_read"] == 60 and row["tokens_cache_write"] == 7 and row["llm"] == 1


def test_from_result_llm_false_and_missing():
    assert tracking.from_result({"llm": False})["llm"] == 0
    assert "llm" not in tracking.from_result({"usage": {"tokens_in": 1}})
    assert "tokens_cache_read" not in tracking.from_result({"usage": {"tokens_in": 1}})
