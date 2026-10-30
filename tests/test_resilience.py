"""Fleet resilience: poller failover on 5xx, checked reporting, stranded-task reaping,
a /health that tests the queue, and cluster-wide worker liveness."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest

from shared.models import Task, TaskStatus, TaskType


# ── poller ───────────────────────────────────────────────────────────────────

_REAL_CLIENT = httpx.AsyncClient   # captured before any patching so helpers never stack


def _poller(monkeypatch, handler, urls=("http://a", "http://b")):
    """A Poller whose HTTP goes to `handler(request) -> httpx.Response`."""
    from worker import poller as pm

    real = _REAL_CLIENT

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(pm.httpx, "AsyncClient", factory)
    monkeypatch.setattr(pm.asyncio, "sleep", _no_sleep)
    return pm.Poller("w1", {}, 1, orchestrator_urls=list(urls))


async def _no_sleep(*_a, **_k):
    return None


@pytest.mark.asyncio
async def test_send_moves_to_next_orchestrator_on_5xx(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(500) if req.url.host == "a" else httpx.Response(200, json={"ok": 1})

    p = _poller(monkeypatch, handler)
    resp = await p._send("GET", "/x")
    assert resp.status_code == 200 and seen == ["a", "b"]
    assert p._pref == 1                       # b is now the preferred endpoint


@pytest.mark.asyncio
async def test_send_returns_last_5xx_when_every_orchestrator_is_unhealthy(monkeypatch):
    p = _poller(monkeypatch, lambda req: httpx.Response(503))
    assert (await p._send("GET", "/x")).status_code == 503


@pytest.mark.asyncio
async def test_report_retries_5xx_but_not_4xx(monkeypatch):
    calls = []

    def flaky(req):
        calls.append(1)
        return httpx.Response(500) if len(calls) < 3 else httpx.Response(200, json={})

    p = _poller(monkeypatch, flaky, urls=("http://a",))
    assert (await p._report("POST", "/tasks/1/complete")).status_code == 200
    assert len(calls) == 3

    calls.clear()
    p = _poller(monkeypatch, lambda req: calls.append(1) or httpx.Response(404), urls=("http://a",))
    with pytest.raises(RuntimeError):
        await p._report("POST", "/tasks/1/complete")
    assert len(calls) == 1                    # a refusal is final, not retried


@pytest.mark.asyncio
async def test_report_raises_after_exhausting_retries(monkeypatch):
    p = _poller(monkeypatch, lambda req: httpx.Response(500), urls=("http://a",))
    with pytest.raises(RuntimeError, match="could not report"):
        await p._report("POST", "/tasks/1/complete")


@pytest.mark.asyncio
async def test_slot_is_freed_when_the_in_progress_update_fails(monkeypatch):
    def down(req):
        raise httpx.ConnectError("down")

    p = _poller(monkeypatch, down)
    t = Task(type=TaskType.run_script, created_by="x")
    p.active_tasks.append(t.id)
    await p._run_task(t)
    assert t.id not in p.active_tasks         # used to leak when the first PATCH raised


@pytest.mark.asyncio
async def test_finished_result_is_spooled_when_reporting_fails(monkeypatch, tmp_path):
    from worker import poller as pm
    monkeypatch.setattr(pm, "_UNREPORTED_DIR", str(tmp_path))

    async def fake_dispatch(task):
        return {"ok": True, "response": "the answer"}

    monkeypatch.setattr(pm, "dispatch", fake_dispatch)

    def handler(req):                          # PATCH in_progress works, everything after fails
        return httpx.Response(200, json={}) if req.method == "PATCH" else httpx.Response(500)

    p = _poller(monkeypatch, handler, urls=("http://a",))
    t = Task(type=TaskType.run_script, created_by="x")
    p.active_tasks.append(t.id)
    await p._run_task(t)
    saved = json.loads((tmp_path / f"{t.id}.json").read_text())
    assert saved["outcome"]["result"]["response"] == "the answer"   # work is not lost
    assert t.id not in p.active_tasks


# ── reaper (sqlite backend) ──────────────────────────────────────────────────

@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    import importlib
    import orchestrator.db as db_mod
    importlib.reload(db_mod)
    return db_mod


@pytest.mark.asyncio
async def test_reap_stale_moves_only_old_claimed_and_in_progress(sqlite_db):
    db = sqlite_db
    old = (datetime.utcnow() - timedelta(hours=10)).isoformat()
    new = datetime.utcnow().isoformat()
    ids = {}
    for name, status, ts in [("old_claimed", "claimed", old), ("old_prog", "in_progress", old),
                             ("new_prog", "in_progress", new), ("old_done", "done", old),
                             ("old_pending", "pending", old)]:
        t = await db.insert_task(Task(type=TaskType.run_script, created_by="x", notes="keep me" if name == "old_prog" else None))
        await db.update_task(t.id, {"status": status})
        import aiosqlite
        async with aiosqlite.connect(db._db_path()) as c:           # backdate: update_task stamps "now"
            await c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, t.id))
            await c.commit()
        ids[name] = t.id

    cutoff = (datetime.utcnow() - timedelta(hours=6)).isoformat()
    moved = await db.reap_stale(cutoff, "STALE: test")
    assert {m["id"] for m in moved} == {ids["old_claimed"], ids["old_prog"]}

    assert (await db.get_task(ids["old_claimed"])).status == TaskStatus.needs_human
    prog = await db.get_task(ids["old_prog"])
    assert prog.status == TaskStatus.needs_human and prog.notes == "keep me | STALE: test"   # notes appended, not replaced
    assert (await db.get_task(ids["new_prog"])).status == TaskStatus.in_progress
    assert (await db.get_task(ids["old_done"])).status == TaskStatus.done
    assert (await db.get_task(ids["old_pending"])).status == TaskStatus.pending
    assert await db.reap_stale(cutoff, "again") == []                # idempotent


# ── orchestrator endpoints ───────────────────────────────────────────────────

@pytest.fixture
def api(sqlite_db, monkeypatch):
    import importlib
    monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "k")
    import orchestrator.main as m
    importlib.reload(m)
    return m


@pytest.mark.asyncio
async def test_health_is_503_when_the_queue_is_unreadable(api, monkeypatch):
    assert (await api.health())["queue"] == "ok"

    async def boom(**_):
        raise RuntimeError("rqlite no leader")

    monkeypatch.setattr(api.db, "list_tasks", boom)
    resp = await api.health()
    assert resp.status_code == 503 and b"no leader" in resp.body


@pytest.mark.asyncio
async def test_heartbeat_marks_worker_alive_without_a_claim(api):
    api._last_seen.clear()
    await api.worker_heartbeat({"worker_name": "thinkpad-x1"}, x_secret_key="k")
    assert "thinkpad-x1" in api._last_seen
    with pytest.raises(Exception):
        await api.worker_heartbeat({"worker_name": "x"}, x_secret_key="wrong")


@pytest.mark.asyncio
async def test_reap_once_uses_the_stale_threshold(api, monkeypatch):
    seen = {}

    async def fake(cutoff, note):
        seen["cutoff"], seen["note"] = cutoff, note
        return []

    monkeypatch.setattr(api.db, "reap_stale", fake)
    monkeypatch.setattr(api, "STALE_TASK_SECS", 3600)
    await api.reap_once()
    age = datetime.utcnow() - datetime.fromisoformat(seen["cutoff"])
    assert timedelta(minutes=59) < age < timedelta(minutes=61)
    assert "STALE" in seen["note"]


# ── codex binary order ───────────────────────────────────────────────────────

def test_codex_prefers_user_install_over_system_binary(monkeypatch, tmp_path):
    from agents import codex_agent
    user = tmp_path / ".npm-global" / "bin"
    user.mkdir(parents=True)
    (user / "codex").write_text("#!/bin/sh\n")
    monkeypatch.setattr(codex_agent.os.path, "expanduser", lambda p: str(tmp_path / p[2:]) if p.startswith("~/") else p)
    monkeypatch.setattr(codex_agent.shutil, "which", lambda n: "/usr/bin/codex")
    real_isfile = codex_agent.os.path.isfile
    monkeypatch.setattr(codex_agent.os.path, "isfile", lambda p: p == "/usr/bin/codex" or real_isfile(p))
    assert codex_agent._find_cli() == str(user / "codex")


# ── routing ──────────────────────────────────────────────────────────────────

def test_agents_are_spread_across_task_kinds():
    from agents.router import route_chain
    first = {k: route_chain(task_kind=k)[0][0] for k in
             ("code", "review", "verify", "research", "draft", "analyze", "plan", "classify")}
    assert first == {"code": "claude", "review": "claude", "verify": "codex", "research": "agy",
                     "draft": "deepseek", "analyze": "deepseek", "plan": "claude", "classify": "claude"}
    assert {"claude", "codex", "agy", "deepseek"} <= set(first.values())


def test_coding_falls_to_a_write_capable_agent_before_read_only_codex():
    from agents.router import route_chain
    names = [a for a, _ in route_chain(task_kind="code")]
    assert names.index("agy") < names.index("codex")


def test_private_research_never_reaches_deepseek():
    from agents.router import route_chain
    assert "deepseek" not in [a for a, _ in route_chain(task_kind="research", sensitivity="private")]
