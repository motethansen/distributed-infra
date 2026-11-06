"""S-8 desk commands through the rqlite backend, on the same fake rqlite HTTP layer over sqlite that
tests/test_scrum_responses.py uses (monkeypatched `_post`). Covers lifecycle, atomic rate limit, pause, audit rows."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
import pytest_asyncio

from orchestrator import db_rqlite as r
from shared.desk_errors import DeskConflict, DeskPaused, DeskRateLimited


def _fields(actor="michael", verb="plan"):
    return {"verb": verb, "project": "demo", "sprint": "2", "story_id": "S-8", "plan_digest": None,
            "evidence": None, "actor": actor, "tier": "T1"}


def _install(path, monkeypatch):
    conn = sqlite3.connect(path)

    async def fake_post(_path, statements, params=None):
        await asyncio.sleep(0)   # yield like a network call, so concurrent callers interleave
        out = []
        for sql, *args in statements:
            cur = conn.execute(sql, args)
            if cur.description:
                out.append({"columns": [d[0] for d in cur.description], "values": [list(x) for x in cur.fetchall()]})
            else:
                out.append({"last_insert_id": cur.lastrowid, "rows_affected": cur.rowcount})
        conn.commit()
        return out

    monkeypatch.setattr(r, "_post", fake_post)
    return conn


@pytest_asyncio.fixture
async def rq(tmp_path, monkeypatch):
    conn = _install(tmp_path / "rq.db", monkeypatch)
    await r.ensure_schema()
    return conn


def _audit(conn, rid=None):
    q = "SELECT response_id, from_status, to_status, actor FROM scrum_audit"
    return conn.execute(q + (" WHERE response_id=? ORDER BY id" if rid else " ORDER BY id"), (rid,) if rid else ()).fetchall()


@pytest.mark.asyncio
async def test_lifecycle_and_audit_attribution(rq):
    row = await r.insert_command(_fields(actor="alice"), "k1", 10)
    rid = row["id"]
    assert row["status"] == "queued" and rid
    assert await r.consume_scrum_response(rid, "worker-1") == "ok"
    assert await r.consume_scrum_response(rid, "worker-2") == "already_consumed"
    assert await r.transition_result(rid, "done", "all good", "worker-1") == "ok"
    for st in ("done", "failed"):
        with pytest.raises(DeskConflict):
            await r.transition_result(rid, st, "again", "worker-9")
    assert await r.transition_result(9999, "done", "", "w") == "not_found"
    # one audit row per real change, attributed to whoever made it; the refused attempts left none
    assert _audit(rq, rid) == [(rid, None, "queued", "alice"), (rid, "queued", "running", "worker-1"),
                               (rid, "running", "done", "worker-1")]
    cmd = (await r.list_commands())[0]
    assert cmd["status"] == "done" and cmd["result_summary"] == "all good" and cmd["actor"] == "alice"


@pytest.mark.asyncio
async def test_idempotent_replay_adds_no_row_and_conflict_raises(rq):
    a = await r.insert_command(_fields(), "same", 10)
    b = await r.insert_command(_fields(), "same", 10)
    assert b["id"] == a["id"] and b.get("replayed")
    with pytest.raises(ValueError):
        await r.insert_command(_fields(verb="validate"), "same", 10)
    assert len(await r.list_commands()) == 1 and len(_audit(rq)) == 1


@pytest.mark.asyncio
async def test_rate_limit_is_atomic_under_concurrency(rq):
    results = await asyncio.gather(*[r.insert_command(_fields(actor="burst"), f"c{i}", 10) for i in range(12)],
                                   return_exceptions=True)
    assert sum(isinstance(x, dict) for x in results) == 10
    assert sum(isinstance(x, DeskRateLimited) for x in results) == 2
    assert rq.execute("SELECT COUNT(*) FROM scrum_responses WHERE kind='command'").fetchone()[0] == 10
    assert len(_audit(rq)) == 10                       # the refused inserts wrote no audit row
    assert (await r.insert_command(_fields(actor="other"), "o1", 10))["id"]   # per actor


@pytest.mark.asyncio
async def test_pause_set_get_blocks_claim_and_persists(rq, tmp_path, monkeypatch):
    assert (await r.get_pause())["paused"] is False
    p = await r.set_pause(True, "michael", "maintenance")
    assert p["paused"] is True and p["by"] == "michael" and p["reason"] == "maintenance"
    queued = (await r.insert_command(_fields(), "p1", 10))["id"]   # still queues while paused
    with pytest.raises(DeskPaused):
        await r.consume_scrum_response(queued, "w")
    # "restart": a fresh connection to the same database, schema re-ensured, pause must survive
    rq.close()
    _install(tmp_path / "rq.db", monkeypatch)
    await r.ensure_schema()
    assert (await r.get_pause())["paused"] is True
    await r.set_pause(False, "bob")
    assert (await r.get_pause())["paused"] is False
    assert await r.consume_scrum_response(queued, "w") == "ok"


@pytest.mark.asyncio
async def test_pause_changes_are_audited_and_audit_is_append_only(rq):
    await r.set_pause(True, "alice")
    await r.set_pause(False, "bob")
    assert _audit(rq) == [(None, "active", "paused", "alice"), (None, "paused", "active", "bob")]
    with pytest.raises(sqlite3.DatabaseError):
        rq.execute("UPDATE scrum_audit SET actor='x'")
    with pytest.raises(sqlite3.DatabaseError):
        rq.execute("DELETE FROM scrum_audit")
