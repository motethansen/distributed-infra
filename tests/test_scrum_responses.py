"""Owner responses to board items: POST/GET/consume, auth, validation, append-only, and persistence across restarts."""
from __future__ import annotations

import importlib
import sqlite3

import httpx
import pytest

BODY = {"item_key": "demo/DM-001/review", "project": "demo", "item_id": "DM-001", "section": "review",
        "action": "confirm", "suggestion_id": "s1", "suggestion_text": "Ship it", "comment": None, "actor": "michael"}
H = {"X-Secret-Key": "k"}


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db")); monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "k")
    monkeypatch.setenv("SCRUM_REPO", str(tmp_path / "repo"))   # never the live notes repo
    import orchestrator.db as dbm; importlib.reload(dbm)
    import orchestrator.main as m; importlib.reload(m)
    return m


def _client(m):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://t")


@pytest.fixture
def api(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_post_records_and_get_lists_newest_first(api):
    async with _client(api) as c:
        r = await c.post("/scrum/responses", json=BODY, headers=H)
        assert r.status_code == 201
        out = r.json()
        assert out["status"] == "recorded" and out["id"] and out["created_at"].endswith("Z")
        await c.post("/scrum/responses", json={**BODY, "item_key": "demo/DM-002/review", "item_id": None}, headers=H)
        rows = (await c.get("/scrum/responses?status=pending", headers=H)).json()
    assert [x["item_key"] for x in rows] == ["demo/DM-002/review", "demo/DM-001/review"]
    row = rows[1]
    for k in ("actor", "created_at", "item_key", "suggestion_id", "suggestion_text", "action", "comment", "status"):
        assert k in row
    assert row["actor"] == "michael" and row["suggestion_text"] == "Ship it"


@pytest.mark.asyncio
async def test_status_pending_filter_matches_unconsumed(api):
    # The story asks for ?status=pending: a freshly recorded row is 'recorded', so 'pending' is an alias for it.
    async with _client(api) as c:
        await c.post("/scrum/responses", json=BODY, headers=H)
        rows = (await c.get("/scrum/responses?status=pending", headers=H)).json()
    assert len(rows) == 1 and rows[0]["status"] == "recorded"


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {"suggestion_id": None},
    {"action": "comment", "comment": ""},
    {"action": "comment", "comment": "   \n"},
    {"action": "comment", "comment": None},
    {"action": "comment", "comment": "x" * 4001},
    {"section": "other"},
    {"action": "approve"},
])
async def test_validation_errors_are_422(api, patch):
    async with _client(api) as c:
        r = await c.post("/scrum/responses", json={**BODY, **patch}, headers=H)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_comment_at_limit_is_accepted(api):
    async with _client(api) as c:
        r = await c.post("/scrum/responses", json={**BODY, "action": "comment", "suggestion_id": None, "comment": "x" * 4000}, headers=H)
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_append_only_and_latest_newer_wins(api):
    async with _client(api) as c:
        await c.post("/scrum/responses", json={**BODY, "action": "comment", "comment": "first"}, headers=H)
        await c.post("/scrum/responses", json={**BODY, "action": "comment", "comment": "second"}, headers=H)
        await c.post("/scrum/responses", json={**BODY, "item_key": "other"}, headers=H)
        allrows = (await c.get("/scrum/responses", headers=H)).json()
        latest = (await c.get("/scrum/responses?latest=1", headers=H)).json()
    assert len(allrows) == 3
    assert len(latest) == 2
    assert {x["item_key"]: x["comment"] for x in latest}[BODY["item_key"]] == "second"


@pytest.mark.asyncio
async def test_survives_restart_with_fresh_db_instance(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    async with _client(m) as c:
        rid = (await c.post("/scrum/responses", json=BODY, headers=H)).json()["id"]
    m2 = _load(tmp_path, monkeypatch)            # fresh modules, same storage = a restart
    async with _client(m2) as c:
        rows = (await c.get("/scrum/responses", headers=H)).json()
    assert [r["id"] for r in rows] == [rid] and rows[0]["actor"] == "michael"


@pytest.mark.asyncio
async def test_consume_once_then_409(api):
    async with _client(api) as c:
        rid = (await c.post("/scrum/responses", json=BODY, headers=H)).json()["id"]
        r = await c.post(f"/scrum/responses/{rid}/consume", headers={**H, "X-Actor": "dis-agent"})
        assert r.status_code == 200
        assert (await c.post(f"/scrum/responses/{rid}/consume", headers=H)).status_code == 409
        assert (await c.post("/scrum/responses/9999/consume", headers=H)).status_code == 404
        row = (await c.get("/scrum/responses?status=consumed", headers=H)).json()[0]
        assert (await c.get("/scrum/responses?status=recorded", headers=H)).json() == []
    assert row["status"] == "consumed" and row["consumed_by"] == "dis-agent" and row["consumed_at"].endswith("Z")


@pytest.mark.asyncio
async def test_idempotency_key_dedupes_a_double_submit(api):
    async with _client(api) as c:
        a = (await c.post("/scrum/responses", json=BODY, headers={**H, "Idempotency-Key": "abc"})).json()
        b = (await c.post("/scrum/responses", json=BODY, headers={**H, "Idempotency-Key": "abc"})).json()
        rows = (await c.get("/scrum/responses", headers=H)).json()
    assert a["id"] == b["id"] and len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"X-Secret-Key": "wrong"}])
async def test_auth_required_on_all_three_endpoints(api, headers):
    async with _client(api) as c:
        assert (await c.post("/scrum/responses", json=BODY, headers=headers)).status_code == 401
        assert (await c.get("/scrum/responses", headers=headers)).status_code == 401
        assert (await c.post("/scrum/responses/1/consume", headers=headers)).status_code == 401


# ── rqlite backend: same behaviour, driven through a fake rqlite HTTP layer over sqlite ─────────────

@pytest.mark.asyncio
async def test_rqlite_backend_matches(tmp_path, monkeypatch):
    from orchestrator import db_rqlite as r
    conn = sqlite3.connect(tmp_path / "rq.db")

    async def fake_post(path, statements, params=None):
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
    await r.ensure_schema()
    await r.ensure_schema()   # idempotent: the desk ALTERs/DDL are guarded
    a = await r.insert_scrum_response(BODY)
    b = await r.insert_scrum_response({**BODY, "action": "comment", "comment": "later"})
    assert b["id"] > a["id"] and a["status"] == "recorded"
    assert [x["id"] for x in await r.list_scrum_responses(status="recorded")] == [b["id"], a["id"]]
    assert [x["id"] for x in await r.list_scrum_responses(latest=True)] == [b["id"]]
    assert await r.consume_scrum_response(a["id"], "me") == "ok"
    assert await r.consume_scrum_response(a["id"], "me") == "already_consumed"
    assert await r.consume_scrum_response(999, "me") == "not_found"
