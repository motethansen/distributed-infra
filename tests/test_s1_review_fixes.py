"""Fixes from the Codex review of S-1: fail-closed auth, 401 before 422, idempotency never drops a distinct
response, and the rqlite backend keeps a response across a fresh start (startup creates the schema)."""
from __future__ import annotations

import importlib
import sqlite3

import httpx
import pytest

from tests.test_scrum_responses import BODY, H, _client, _load


@pytest.fixture
def api(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_routes_fail_closed_when_no_secret_key_is_configured(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "SECRET_KEY", "")
    async with _client(m) as c:
        for headers in ({}, {"X-Secret-Key": ""}, {"X-Secret-Key": "anything"}):
            assert (await c.get("/scrum/responses", headers=headers)).status_code == 503
            assert (await c.post("/scrum/responses", json=BODY, headers=headers)).status_code == 503
            assert (await c.post("/scrum/responses/1/consume", headers=headers)).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"X-Secret-Key": "wrong"}])
@pytest.mark.parametrize("body", [{}, {"item_key": ""}, {"action": "nonsense"}])
async def test_bad_key_is_401_even_when_the_body_is_invalid(api, headers, body):
    async with _client(api) as c:
        assert (await c.post("/scrum/responses", json=body, headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_valid_key_with_invalid_body_is_still_422(api):
    async with _client(api) as c:
        assert (await c.post("/scrum/responses", json={}, headers=H)).status_code == 422


@pytest.mark.asyncio
async def test_same_idempotency_key_with_a_different_response_is_a_conflict_not_a_discard(api):
    other = {**BODY, "item_key": "demo/DM-002/review", "item_id": "DM-002", "actor": "ana"}
    async with _client(api) as c:
        first = await c.post("/scrum/responses", json=BODY, headers={**H, "Idempotency-Key": "k1"})
        clash = await c.post("/scrum/responses", json=other, headers={**H, "Idempotency-Key": "k1"})
        rows = (await c.get("/scrum/responses", headers=H)).json()
    assert first.status_code == 201 and clash.status_code == 409
    assert len(rows) == 1 and rows[0]["item_key"] == BODY["item_key"]


@pytest.mark.asyncio
async def test_identical_replay_returns_the_original_answer_even_after_it_was_consumed(api):
    async with _client(api) as c:
        a = (await c.post("/scrum/responses", json=BODY, headers={**H, "Idempotency-Key": "k2"})).json()
        assert (await c.post(f"/scrum/responses/{a['id']}/consume", headers=H)).status_code == 200
        r = await c.post("/scrum/responses", json=BODY, headers={**H, "Idempotency-Key": "k2"})
        rows = (await c.get("/scrum/responses", headers=H)).json()
    assert r.status_code == 201 and r.json()["id"] == a["id"] and r.json()["status"] == "recorded"
    assert len(rows) == 1


def _fake_rqlite(path, monkeypatch, module):
    conn = sqlite3.connect(path)

    async def fake_post(p, statements, params=None):
        out = []
        for sql, *args in statements:
            cur = conn.execute(sql, args)
            if cur.description:
                out.append({"columns": [d[0] for d in cur.description], "values": [list(x) for x in cur.fetchall()]})
            else:
                out.append({"last_insert_id": cur.lastrowid, "rows_affected": cur.rowcount})
        conn.commit()
        return out

    monkeypatch.setattr(module, "_post", fake_post)
    return conn


@pytest.mark.asyncio
async def test_rqlite_startup_creates_the_schema_and_a_fresh_instance_still_has_the_response(tmp_path, monkeypatch):
    from orchestrator import db_rqlite
    path = str(tmp_path / "shared.db")

    first = importlib.reload(db_rqlite)                 # "process 1"
    conn1 = _fake_rqlite(path, monkeypatch, first)
    await first.ensure_schema()                         # what the app does at startup
    await first.ensure_schema()                         # a second start must not fail
    rec = await first.insert_scrum_response(BODY)
    conn1.close()

    second = importlib.reload(db_rqlite)                # "process 2", after a restart
    conn2 = _fake_rqlite(path, monkeypatch, second)
    await second.ensure_schema()
    rows = await second.list_scrum_responses()
    conn2.close()
    assert [r["id"] for r in rows] == [rec["id"]] and rows[0]["actor"] == BODY["actor"]


@pytest.mark.asyncio
async def test_rqlite_idempotency_conflict_is_raised(tmp_path, monkeypatch):
    from orchestrator import db_rqlite
    mod = importlib.reload(db_rqlite)
    _fake_rqlite(str(tmp_path / "i.db"), monkeypatch, mod)
    await mod.ensure_schema()
    await mod.insert_scrum_response(BODY, "same")
    assert (await mod.insert_scrum_response(BODY, "same"))["replayed"] is True
    with pytest.raises(ValueError):
        await mod.insert_scrum_response({**BODY, "actor": "someone-else"}, "same")
