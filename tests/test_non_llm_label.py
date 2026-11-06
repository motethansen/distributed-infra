"""Non-LLM task types are stored agent='none', llm=false; LLM task types llm=true (both back ends)."""
from __future__ import annotations

import pytest

from shared import tracking
from shared.models import Task, TaskStatus, TaskType
from tests.test_usage_columns import backend  # noqa: F401  (sqlite + rqlite fixture)


async def _finish(backend, ttype, result):
    db = backend[0]
    if hasattr(db, "ensure_schema"):
        await db.ensure_schema()
    t = await db.insert_task(Task(type=ttype, created_by="t"))
    await db.update_task(t.id, {"status": TaskStatus.done, "result": result})
    return await db.get_task(t.id)


@pytest.mark.asyncio
async def test_non_llm_task_is_stored_as_none_and_not_llm(backend):  # noqa: F811
    t = await _finish(backend, TaskType.run_script, {"ok": True, "stdout": "hi"})
    assert t.agent == "none" and t.llm is False


@pytest.mark.asyncio
async def test_llm_task_is_stored_llm_true(backend):  # noqa: F811
    t = await _finish(backend, TaskType.agent_run, {"ok": True, "agent": "claude", "usage": {"tokens_in": 3}})
    assert t.llm is True and t.agent == "claude"


def test_type_sets_are_disjoint_and_known():
    assert not tracking.NON_LLM_TYPES & tracking.LLM_TYPES
    known = {t.value for t in TaskType}
    assert (tracking.NON_LLM_TYPES | tracking.LLM_TYPES) <= known


def test_mixed_type_left_unset():
    assert "llm" not in tracking.for_task("find", {"ok": True})
