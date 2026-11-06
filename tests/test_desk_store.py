"""Desk command rows: columns migration, queue/claim/result transitions, pause, audit, rate count, listing."""
from __future__ import annotations

import importlib
import sqlite3

import pytest

from shared.desk_errors import DeskConflict, DeskPaused

CMD = {"project": "demo", "actor": "michael", "verb": "go", "sprint": "s1", "story_id": "S-8",
       "plan_digest": "abc", "evidence": "ok", "tier": "T1"}
RESP = {"item_key": "demo/DM-1/review", "project": "demo", "section": "review", "action": "confirm", "actor": "michael"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    import orchestrator.db as dbm
    importlib.reload(dbm)
    dbm._path = str(tmp_path / "q.db")
    return dbm


def _audit(db):
    con = sqlite3.connect(db.DB_PATH)
    return con.execute("SELECT response_id, from_status, to_status, actor FROM scrum_audit ORDER BY id").fetchall()


@pytest.mark.asyncio
async def test_old_rows_read_as_response_and_migration_is_idempotent(db):
    con = sqlite3.connect(db.DB_PATH)
    con.executescript("".join(s + ";" for s in db.SCRUM_RESPONSES_DDL[:1]))
    con.execute("INSERT INTO scrum_responses (item_key, project, section, action, actor, created_at) "
                "VALUES ('k','p','s','confirm','a','2026-01-01T00:00:00.000000Z')")
    con.commit(); con.close()
    rows = await db.list_scrum_responses()
    assert rows[0]["kind"] == "response"
    db._MIGRATED.clear()
    assert (await db.list_scrum_responses())[0]["kind"] == "response"   # second migration: no duplicate-column error


@pytest.mark.asyncio
async def test_insert_command_idempotency(db):
    a = await db.insert_command(CMD, "k1")
    assert a["status"] == "queued"
    assert (await db.insert_command(CMD, "k1"))["id"] == a["id"]
    with pytest.raises(ValueError):
        await db.insert_command({**CMD, "verb": "stop"}, "k1")


@pytest.mark.asyncio
async def test_claim_once_then_result_once(db):
    cid = (await db.insert_command(CMD))["id"]
    assert await db.consume_scrum_response(cid, "runner") == "ok"
    assert await db.consume_scrum_response(cid, "runner") == "already_consumed"
    with pytest.raises(ValueError):
        await db.transition_result(cid, "done", "x" * 2001)
    with pytest.raises(ValueError):
        await db.transition_result(cid, "queued", "x")
    assert await db.transition_result(cid, "done", "fine", "runner") == "ok"
    with pytest.raises(DeskConflict):
        await db.transition_result(cid, "failed", "again")
    assert await db.transition_result(99999, "done", "x") == "not_found"


@pytest.mark.asyncio
async def test_result_requires_running(db):
    cid = (await db.insert_command(CMD))["id"]
    with pytest.raises(DeskConflict):
        await db.transition_result(cid, "done", "never claimed")
    rid = (await db.insert_scrum_response(RESP))["id"]
    with pytest.raises(DeskConflict):
        await db.transition_result(rid, "done", "not a command")


@pytest.mark.asyncio
async def test_pause_blocks_next_claim_only(db):
    running = (await db.insert_command(CMD))["id"]
    queued = (await db.insert_command({**CMD, "verb": "stop"}))["id"]
    await db.consume_scrum_response(running, "runner")
    p = await db.set_pause(True, "michael", "maintenance")
    assert p["paused"] and p["by"] == "michael" and p["reason"] == "maintenance" and p["at"]
    with pytest.raises(DeskPaused):
        await db.consume_scrum_response(queued, "runner")
    assert await db.transition_result(running, "done", "finished while paused") == "ok"
    assert (await db.set_pause(False, "michael"))["paused"] is False
    assert await db.consume_scrum_response(queued, "runner") == "ok"


@pytest.mark.asyncio
async def test_pause_survives_restart_and_responses_unaffected(db):
    await db.set_pause(True, "michael")
    importlib.reload(db)
    assert (await db.get_pause())["paused"] is True
    rid = (await db.insert_scrum_response(RESP))["id"]
    assert await db.consume_scrum_response(rid, "x") == "ok"   # responses are not gated by the pause


@pytest.mark.asyncio
async def test_audit_is_written_and_append_only(db):
    cid = (await db.insert_command(CMD))["id"]
    await db.consume_scrum_response(cid, "runner")
    await db.consume_scrum_response(cid, "runner")             # failed claim: no audit row
    await db.transition_result(cid, "failed", "boom", "runner")
    await db.set_pause(True, "michael")
    assert _audit(db) == [(cid, None, "queued", "michael"), (cid, "queued", "running", "runner"),
                          (cid, "running", "failed", "runner"), (None, "active", "paused", "michael")]
    con = sqlite3.connect(db.DB_PATH)
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE scrum_audit SET actor='x'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM scrum_audit")


@pytest.mark.asyncio
async def test_rate_count_and_list(db):
    for _ in range(3):
        await db.insert_command(CMD)
    await db.insert_command({**CMD, "actor": "other"})
    await db.insert_scrum_response(RESP)
    assert await db.count_commands_last_hour("michael") == 3
    assert await db.count_commands_last_hour("nobody") == 0
    con = sqlite3.connect(db.DB_PATH)
    con.execute("UPDATE scrum_responses SET created_at='2020-01-01T00:00:00.000000Z' WHERE id=1"); con.commit()
    assert await db.count_commands_last_hour("michael") == 2
    cmds = await db.list_commands()
    assert len(cmds) == 4 and set(cmds[0]) == {"id", "status", "tier", "actor", "created_at", "verb", "result_summary"}
