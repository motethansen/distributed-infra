"""S-8 acceptance (independent): typed desk command intents, allowlist tiers, lifecycle, rate limit, pause, audit.
Driven through the HTTP API only, on a throwaway sqlite queue DB."""
from __future__ import annotations

import importlib
import sqlite3

import httpx
import pytest

H = {"X-Secret-Key": "k"}
T1 = ["refresh_proposal", "plan", "validate", "step_done"]
T2 = ["approve", "go", "po_request", "po_revise", "accept"]
T3 = ["merge", "deploy", "cancel", "close"]


def _intent(verb="plan", **kw):
    b = {"kind": "command", "verb": verb, "project": "demo", "sprint": "2", "story_id": "S-8",
         "plan_digest": None, "evidence": None, "actor": "michael"}
    if verb == "step_done":
        b["evidence"] = "tests green"
    b.update(kw)
    return b


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db")); monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "k")
    monkeypatch.setenv("SCRUM_REPO", str(tmp_path / "repo"))
    import orchestrator.db as dbm; importlib.reload(dbm)
    import orchestrator.main as m; importlib.reload(m)
    return m


def _client(m):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://t")


@pytest.fixture
def api(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


_n = [0]


async def _post(c, body, key=None):
    _n[0] += 1
    return await c.post("/scrum/responses", json=body, headers={**H, "Idempotency-Key": key or f"k{_n[0]}"})


# 1
@pytest.mark.asyncio
async def test_c1_valid_command_201_queued_with_tier(api):
    async with _client(api) as c:
        r = await _post(c, _intent("plan"))
    assert r.status_code == 201
    j = r.json()
    assert j["id"] and j["status"] == "queued" and j["tier"] == "T1"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"command": "rm -rf /"}, {"args": "--force"}, {"foo": "bar"}, {"args": ["x"]}])
async def test_c1_extra_or_freetext_field_422(api, extra):
    async with _client(api) as c:
        r = await _post(c, {**_intent("plan"), **extra})
    assert r.status_code == 422


# 2
@pytest.mark.asyncio
@pytest.mark.parametrize("verb", T1)
async def test_c2_t1_verbs_201(api, verb):
    async with _client(api) as c:
        r = await _post(c, _intent(verb))
    assert r.status_code == 201 and r.json()["tier"] == "T1"


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", T2)
async def test_c2_t2_verbs_403_needs_confirm(api, verb):
    async with _client(api) as c:
        r = await _post(c, _intent(verb))
    assert r.status_code == 403
    assert "needs confirm-with-digest (sprint 3)" in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", T3)
async def test_c2_t3_verbs_403_not_available(api, verb):
    async with _client(api) as c:
        r = await _post(c, _intent(verb))
    assert r.status_code == 403
    assert "not available from the page" in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["rm", "shell", "", "PLAN", "exec"])
async def test_c2_unknown_verb_422(api, verb):
    async with _client(api) as c:
        r = await _post(c, _intent(verb))
    assert r.status_code == 422


def test_c2_allowlist_module_tiers():
    from shared import desk_allowlist as al
    text = open(al.__file__).read()
    for v in T1 + T2 + T3:
        assert v in text


# 3
@pytest.mark.asyncio
@pytest.mark.parametrize("ev", [None, "", "   "])
async def test_c3_step_done_needs_evidence(api, ev):
    async with _client(api) as c:
        r = await _post(c, _intent("step_done", evidence=ev))
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["project", "sprint", "story_id"])
@pytest.mark.parametrize("bad", ["; rm -rf", "../x", "a b", "x" * 65, "", "a/b", "a;b"])
async def test_c3_bad_identifiers_422(api, field, bad):
    async with _client(api) as c:
        r = await _post(c, _intent("plan", **{field: bad}))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_c3_good_identifiers_ok(api):
    async with _client(api) as c:
        r = await _post(c, _intent("plan", project="a.b_c-1", sprint="x" * 64, story_id="S-8.1"))
    assert r.status_code == 201


# 4
async def _queued(c, verb="plan", **kw):
    return (await _post(c, _intent(verb, **kw))).json()["id"]


@pytest.mark.asyncio
async def test_c4_lifecycle_done(api):
    async with _client(api) as c:
        i = await _queued(c)
        assert (await c.post(f"/scrum/responses/{i}/consume", headers=H)).status_code == 200
        assert (await c.post(f"/scrum/responses/{i}/consume", headers=H)).status_code == 409
        r = await c.post(f"/scrum/responses/{i}/result", json={"status": "done", "summary": "ok"}, headers=H)
        assert r.status_code == 200
        rows = (await c.get("/scrum/responses?kind=command", headers=H)).json()
    assert [x for x in rows if x["id"] == i][0]["status"] == "done"


@pytest.mark.asyncio
async def test_c4_lifecycle_failed_and_running_visible(api):
    async with _client(api) as c:
        i = await _queued(c)
        await c.post(f"/scrum/responses/{i}/consume", headers=H)
        rows = (await c.get("/scrum/responses?kind=command", headers=H)).json()
        assert [x for x in rows if x["id"] == i][0]["status"] == "running"
        r = await c.post(f"/scrum/responses/{i}/result", json={"status": "failed", "summary": "boom"}, headers=H)
        assert r.status_code == 200
        rows = (await c.get("/scrum/responses?kind=command", headers=H)).json()
    assert [x for x in rows if x["id"] == i][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_c4_result_from_queued_is_409(api):
    async with _client(api) as c:
        i = await _queued(c)
        r = await c.post(f"/scrum/responses/{i}/result", json={"status": "done", "summary": "x"}, headers=H)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_c4_second_result_is_409(api):
    async with _client(api) as c:
        i = await _queued(c)
        await c.post(f"/scrum/responses/{i}/consume", headers=H)
        await c.post(f"/scrum/responses/{i}/result", json={"status": "done", "summary": "x"}, headers=H)
        for st in ("done", "failed"):
            r = await c.post(f"/scrum/responses/{i}/result", json={"status": st, "summary": "y"}, headers=H)
            assert r.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("st", ["queued", "running", "bogus"])
async def test_c4_result_status_other_than_done_failed_is_409(api, st):
    async with _client(api) as c:
        i = await _queued(c)
        await c.post(f"/scrum/responses/{i}/consume", headers=H)
        r = await c.post(f"/scrum/responses/{i}/result", json={"status": st, "summary": "x"}, headers=H)
    assert r.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"summary": "x"}, {"status": 5, "summary": "x"}, {"status": "done", "summary": "x" * 2001},
                                  {"status": "done", "extra": 1}])
async def test_c4_malformed_result_body_is_422(api, body):
    async with _client(api) as c:
        i = await _queued(c)
        await c.post(f"/scrum/responses/{i}/consume", headers=H)
        r = await c.post(f"/scrum/responses/{i}/result", json=body, headers=H)
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("path, to", [("done", "running"), ("failed", "done"), ("queued", "done"),
                                      ("queued", "failed"), ("running", "queued"), ("done", "queued"),
                                      ("failed", "running")])
async def test_c4_every_other_transition_is_409(api, path, to):
    """Lifecycle is queued -> running -> done|failed only; reach `path`, then try to move to `to`."""
    async with _client(api) as c:
        i = await _queued(c)
        if path != "queued":
            await c.post(f"/scrum/responses/{i}/consume", headers=H)
        if path in ("done", "failed"):
            assert (await c.post(f"/scrum/responses/{i}/result", json={"status": path, "summary": ""}, headers=H)).status_code == 200
        r = await c.post(f"/scrum/responses/{i}/result", json={"status": to, "summary": "x"}, headers=H)
        assert r.status_code == 409
        state = [x for x in (await c.get("/scrum/responses?kind=command", headers=H)).json() if x["id"] == i][0]
    assert state["status"] == path


@pytest.mark.asyncio
async def test_c4_summary_limit_2000(api):
    async with _client(api) as c:
        i = await _queued(c)
        await c.post(f"/scrum/responses/{i}/consume", headers=H)
        r = await c.post(f"/scrum/responses/{i}/result", json={"status": "done", "summary": "x" * 2001}, headers=H)
        assert r.status_code == 422
        r = await c.post(f"/scrum/responses/{i}/result", json={"status": "done", "summary": "x" * 2000}, headers=H)
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_c4_result_requires_secret_key(api):
    async with _client(api) as c:
        i = await _queued(c)
        await c.post(f"/scrum/responses/{i}/consume", headers=H)
        r = await c.post(f"/scrum/responses/{i}/result", json={"status": "done", "summary": "x"})
    assert r.status_code in (401, 403)


# 5
@pytest.mark.asyncio
async def test_c5_rate_limit_11th_is_429_per_actor(api):
    async with _client(api) as c:
        for n in range(10):
            assert (await _post(c, _intent("plan", actor="rl"))).status_code == 201, n
        assert (await _post(c, _intent("plan", actor="rl"))).status_code == 429
        assert (await _post(c, _intent("plan", actor="other"))).status_code == 201


@pytest.mark.asyncio
async def test_c5_rate_limit_holds_under_concurrency(api, monkeypatch):
    """12 simultaneous requests from one actor: exactly 10 get in, 2 are refused, and only 10 rows exist."""
    import asyncio
    real_count = api.db.count_commands_last_hour

    async def slow_count(actor):   # widens any check-then-insert window; unused by the atomic path
        n = await real_count(actor)
        await asyncio.sleep(0.05)
        return n
    monkeypatch.setattr(api.db, "count_commands_last_hour", slow_count)
    async with _client(api) as c:
        rs = await asyncio.gather(*[_post(c, _intent("plan", actor="burst")) for _ in range(12)])
        rows = (await c.get("/scrum/responses?kind=command", headers=H)).json()
    codes = sorted(r.status_code for r in rs)
    assert codes == [201] * 10 + [429] * 2
    assert len([r for r in rows if r["actor"] == "burst"]) == 10
    con = sqlite3.connect(api.db._db_path())
    assert con.execute("SELECT COUNT(*) FROM scrum_audit WHERE actor='burst'").fetchone()[0] == 10


@pytest.mark.asyncio
async def test_c5_replay_at_the_limit_returns_original_not_429(api):
    async with _client(api) as c:
        first = await _post(c, _intent("plan", actor="rl2"), key="same")
        for _ in range(9):
            await _post(c, _intent("plan", actor="rl2"))
        again = await _post(c, _intent("plan", actor="rl2"), key="same")
        extra = await _post(c, _intent("plan", actor="rl2"))
    assert again.status_code == 201 and again.json()["id"] == first.json()["id"]
    assert extra.status_code == 429


@pytest.mark.asyncio
async def test_c5_same_key_concurrent_requests_make_one_command(api, monkeypatch):
    """8 simultaneous requests with ONE Idempotency-Key: one row, one audit row, one id for all."""
    import asyncio
    import aiosqlite
    real = aiosqlite.Connection.execute

    def slow_execute(self, sql, *a, **kw):   # widens a lookup-then-insert window; unused by the atomic path
        coro = real(self, sql, *a, **kw)
        if not str(sql).startswith("SELECT id, created_at, status, kind"):
            return coro

        async def delayed():
            out = await coro
            await asyncio.sleep(0.05)
            return out
        return delayed()
    monkeypatch.setattr(aiosqlite.Connection, "execute", slow_execute)
    async with _client(api) as c:
        rs = await asyncio.gather(*[_post(c, _intent("plan", actor="dup"), key="one") for _ in range(8)])
    assert {r.status_code for r in rs} <= {200, 201}
    assert len({r.json()["id"] for r in rs}) == 1
    con = sqlite3.connect(api.db._db_path())
    assert con.execute("SELECT COUNT(*) FROM scrum_responses WHERE kind='command' AND actor='dup'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM scrum_audit WHERE actor='dup'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_c5_same_key_retry_at_limit_replays_but_new_key_is_429(api):
    async with _client(api) as c:
        first = await _post(c, _intent("plan", actor="lim"), key="k0")
        for n in range(9):
            await _post(c, _intent("plan", actor="lim"), key=f"f{n}")
        again = await _post(c, _intent("plan", actor="lim"), key="k0")
        fresh = await _post(c, _intent("plan", actor="lim"), key="brand-new")
    assert again.json()["id"] == first.json()["id"] and again.status_code in (200, 201)
    assert fresh.status_code == 429


# 6
@pytest.mark.asyncio
async def test_c6_pause_get_post_shape(api):
    async with _client(api) as c:
        g = await c.get("/scrum/desk/pause", headers=H)
        assert g.status_code == 200 and g.json()["paused"] is False
        p = await c.post("/scrum/desk/pause", json={"paused": True, "by": "michael", "reason": "maintenance"}, headers=H)
        assert p.status_code == 200
        j = (await c.get("/scrum/desk/pause", headers=H)).json()
    assert j["paused"] is True and j["by"] == "michael" and j["reason"] == "maintenance" and j["at"]


@pytest.mark.asyncio
async def test_c6_pause_requires_key(api):
    async with _client(api) as c:
        assert (await c.get("/scrum/desk/pause")).status_code in (401, 403)
        assert (await c.post("/scrum/desk/pause", json={"paused": True, "by": "x", "reason": "r"})).status_code in (401, 403)


@pytest.mark.asyncio
async def test_c6_paused_still_queues_but_consume_423(api):
    async with _client(api) as c:
        await c.post("/scrum/desk/pause", json={"paused": True, "by": "m", "reason": "r"}, headers=H)
        r = await _post(c, _intent("plan"))
        assert r.status_code == 201 and r.json()["status"] == "queued"
        i = r.json()["id"]
        r = await c.post(f"/scrum/responses/{i}/consume", headers=H)
        assert r.status_code == 423 and "desk paused" in r.text
        await c.post("/scrum/desk/pause", json={"paused": False, "by": "m", "reason": "done"}, headers=H)
        assert (await c.post(f"/scrum/responses/{i}/consume", headers=H)).status_code == 200


@pytest.mark.asyncio
async def test_c6_pause_survives_restart(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    async with _client(m) as c:
        await c.post("/scrum/desk/pause", json={"paused": True, "by": "michael", "reason": "hold"}, headers=H)
    m2 = _load(tmp_path, monkeypatch)   # same DB file, fresh modules = restart
    async with _client(m2) as c:
        j = (await c.get("/scrum/desk/pause", headers=H)).json()
    assert j["paused"] is True and j["by"] == "michael" and j["reason"] == "hold"


@pytest.mark.asyncio
async def test_c6_fresh_db_not_paused(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    async with _client(m) as c:
        assert (await c.get("/scrum/desk/pause", headers=H)).json()["paused"] is False


# 7
@pytest.mark.asyncio
async def test_c7_list_command_fields(api):
    async with _client(api) as c:
        i = await _queued(c, actor="alice")
        await c.post(f"/scrum/responses/{i}/consume", headers={**H, "X-Actor": "worker"})
        await c.post(f"/scrum/responses/{i}/result", json={"status": "done", "summary": "SUMMARY-X"}, headers={**H, "X-Actor": "worker"})
        await c.post("/scrum/responses", json={"item_key": "a/b/c", "project": "demo", "action": "confirm", "actor": "z"},
                     headers={**H, "Idempotency-Key": "resp1"})
        rows = (await c.get("/scrum/responses?kind=command", headers=H)).json()
    assert len(rows) == 1
    r = rows[0]
    for k in ("status", "tier", "actor", "created_at"):
        assert k in r
    assert r["actor"] == "alice" and r["tier"] == "T1" and r["status"] == "done"
    assert "SUMMARY-X" in str(r)


@pytest.mark.asyncio
async def test_c7_audit_appended_for_each_state_change(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    async with _client(m) as c:
        i = await _queued(c, actor="alice")
        await c.post(f"/scrum/responses/{i}/consume", headers={**H, "X-Actor": "worker1"})
        await c.post(f"/scrum/responses/{i}/result", json={"status": "done", "summary": "s"}, headers={**H, "X-Actor": "worker1"})
        await c.post("/scrum/desk/pause", json={"paused": True, "by": "pauser", "reason": "r"}, headers=H)
    con = sqlite3.connect(tmp_path / "q.db")
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    audit = [t for t in tables if "audit" in t.lower()]
    assert audit, tables
    cols = [r[1] for r in con.execute(f"pragma table_info({audit[0]})")]
    rows = con.execute(f"select * from {audit[0]}").fetchall()
    blob = " ".join(str(x) for r in rows for x in r)
    assert len(rows) >= 4          # queued, running, done, pause
    assert "alice" in blob and "worker1" in blob and "pauser" in blob
    assert any("at" in c_ or "time" in c_ for c_ in cols)
