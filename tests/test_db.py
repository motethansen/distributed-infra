"""Tests for orchestrator/db.py using an isolated tmp SQLite DB."""
from __future__ import annotations

import os
import pytest
import pytest_asyncio

from shared.models import Task, TaskStatus, TaskType


# Point every db call at a fresh tmp file for the duration of the test session.
@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "test_queue.db"))
    # Re-import so the module-level DB_PATH picks up the new env var.
    import importlib
    import orchestrator.db as db_mod
    importlib.reload(db_mod)
    yield


def _make_task(**kwargs) -> Task:
    defaults = dict(type=TaskType.run_script, created_by="test-machine")
    defaults.update(kwargs)
    return Task(**defaults)


# ── insert_task / get_task round-trip ────────────────────────────────────────

@pytest.mark.asyncio
async def test_insert_and_get_task():
    import orchestrator.db as db
    task = _make_task(notes="hello")
    await db.insert_task(task)
    fetched = await db.get_task(task.id)
    assert fetched is not None
    assert fetched.id == task.id
    assert fetched.type == TaskType.run_script
    assert fetched.notes == "hello"
    assert fetched.status == TaskStatus.pending


@pytest.mark.asyncio
async def test_get_task_missing_returns_none():
    import orchestrator.db as db
    result = await db.get_task("nonexistent-id")
    assert result is None


# ── list_tasks ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_tasks_no_filter():
    import orchestrator.db as db
    t1 = _make_task()
    t2 = _make_task(type=TaskType.git_pull)
    await db.insert_task(t1)
    await db.insert_task(t2)
    tasks = await db.list_tasks()
    ids = {t.id for t in tasks}
    assert t1.id in ids
    assert t2.id in ids


@pytest.mark.asyncio
async def test_list_tasks_status_filter():
    import orchestrator.db as db
    pending = _make_task()
    await db.insert_task(pending)
    await db.update_task(pending.id, {"status": TaskStatus.done})

    done_tasks = await db.list_tasks(status=TaskStatus.done)
    pending_tasks = await db.list_tasks(status=TaskStatus.pending)

    assert any(t.id == pending.id for t in done_tasks)
    assert not any(t.id == pending.id for t in pending_tasks)


# ── claim_next_task ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_claim_next_task_empty_capabilities_returns_none():
    import orchestrator.db as db
    task = _make_task()
    await db.insert_task(task)
    result = await db.claim_next_task("worker-a", [])
    assert result is None


@pytest.mark.asyncio
async def test_claim_next_task_wrong_worker_cannot_claim_targeted_task():
    """A task targeted at 'mac-mini' must NOT be claimable by 'thinkpad-x1'."""
    import orchestrator.db as db
    task = _make_task(payload={"_target_machine": "mac-mini"})
    await db.insert_task(task)
    result = await db.claim_next_task("thinkpad-x1", [TaskType.run_script])
    assert result is None


@pytest.mark.asyncio
async def test_claim_next_task_right_worker_claims_targeted_task():
    """A task targeted at 'mac-mini' MUST be claimable by 'mac-mini'."""
    import orchestrator.db as db
    task = _make_task(payload={"_target_machine": "mac-mini"})
    await db.insert_task(task)
    result = await db.claim_next_task("mac-mini", [TaskType.run_script])
    assert result is not None
    assert result.id == task.id
    assert result.status == TaskStatus.claimed
    assert result.assigned_to == "mac-mini"


@pytest.mark.asyncio
async def test_claim_next_task_untargeted_claimable_by_any_capable_worker():
    import orchestrator.db as db
    task = _make_task()  # no _target_machine in payload
    await db.insert_task(task)
    result = await db.claim_next_task("thinkpad-x1", [TaskType.run_script])
    assert result is not None
    assert result.id == task.id


@pytest.mark.asyncio
async def test_claim_next_task_respects_capabilities():
    """Worker without the required capability must not claim the task."""
    import orchestrator.db as db
    task = _make_task(type=TaskType.ios_build)
    await db.insert_task(task)
    result = await db.claim_next_task("thinkpad-x1", [TaskType.android_build])
    assert result is None


# ── update_task ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_task_changes_fields_and_bumps_updated_at():
    import orchestrator.db as db
    task = _make_task()
    await db.insert_task(task)
    original = await db.get_task(task.id)

    updated = await db.update_task(task.id, {"status": TaskStatus.done, "notes": "finished"})
    assert updated is not None
    assert updated.status == TaskStatus.done
    assert updated.notes == "finished"
    assert updated.updated_at > original.updated_at


@pytest.mark.asyncio
async def test_update_task_missing_returns_none():
    import orchestrator.db as db
    result = await db.update_task("no-such-id", {"status": TaskStatus.done})
    assert result is None
