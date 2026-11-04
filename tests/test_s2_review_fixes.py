"""S-2 review fixes: a tool-less model call, no CLI left running after a cancel, one run generates every missing
suggestion, bounded cache lookups, and suggestions persisting in rqlite."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time

import pytest

from agents import claude_agent
from shared import scrum_digest as sd, scrum_suggest
from tests.test_scrum_digest import TODAY, item, proj
from tests.test_scrum_suggest import Counting, digest, load, store  # noqa: F401  (store is a fixture)


def board(n: int) -> dict:
    items = [item(f"VN-{i:03d}", "in_review", f"PR {i}") for i in range(n)]
    return sd.build([proj("vantage", {"in_review": n}, items)], TODAY)


# ── 1. the suggestion call has no tools and no permission bypass ───────────────────────────────

class _Proc:
    returncode = 0

    async def communicate(self):
        return b'{"type":"result","result":"fine","is_error":false}', b""


@pytest.mark.asyncio
@pytest.mark.parametrize("tools_off", [True, False])
async def test_tools_off_removes_every_tool_and_the_permission_bypass(monkeypatch, tools_off):
    seen = {}

    async def fake_exec(*args, **kw):
        seen["args"] = list(args)
        return _Proc()

    monkeypatch.setattr(claude_agent, "_find_cli", lambda: "/bin/claude")
    monkeypatch.setattr(claude_agent.asyncio, "create_subprocess_exec", fake_exec)
    await claude_agent.run("hi", "sonnet", tools_off=tools_off)
    a = seen["args"]
    if tools_off:
        assert a[a.index("--tools") + 1] == "" and "--dangerously-skip-permissions" not in a
        assert "--strict-mcp-config" in a                  # --tools "" alone leaves the connectors (Gmail, Calendar ...) callable
        assert "--disable-slash-commands" in a
    else:
        assert "--tools" not in a and "--dangerously-skip-permissions" in a      # every other caller is unchanged
        assert "--strict-mcp-config" not in a


@pytest.mark.asyncio
async def test_the_orchestrator_asks_the_model_without_tools(monkeypatch):
    from orchestrator import main
    seen = {}

    async def fake_run(prompt, model, **kw):
        seen.update(kw)
        return {"ok": True, "response": "Ask the owner to decide today."}

    monkeypatch.setattr(claude_agent, "run", fake_run)
    assert await main._suggest_call("Item: x") == "Ask the owner to decide today."
    assert seen["tools_off"] is True


# ── 2. a cancelled or timed-out call leaves no CLI process behind ──────────────────────────────

def _fake_cli(tmp_path, pidfile) -> str:
    p = tmp_path / "claude"
    p.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 30\n")
    p.chmod(0o755)
    return str(p)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["cancelled by an outer timeout", "own timeout"])
async def test_a_cancelled_or_timed_out_call_leaves_no_cli_process(tmp_path, monkeypatch, how):
    pidfile = tmp_path / "pid"
    monkeypatch.setattr(claude_agent, "_find_cli", lambda: _fake_cli(tmp_path, pidfile))
    if how.startswith("cancelled"):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(claude_agent.run("x", "sonnet", tools_off=True, timeout=30), 0.8)
    else:
        r = await claude_agent.run("x", "sonnet", tools_off=True, timeout=1)
        assert r["ok"] is False and "timed out" in r["error"]
    assert not _alive(int(pidfile.read_text()))


# ── 3. one background run generates every missing suggestion ───────────────────────────────────

@pytest.mark.asyncio
async def test_eight_items_are_all_generated_in_one_run_and_the_next_load_makes_no_calls(store):  # noqa: F811
    call = Counting()
    n = len(scrum_suggest._entries(board(8)))                # the 8 review items plus the board's own adjustment
    assert n > 6                                             # more than the old six-call cap
    await load(board(8), store, call)
    assert call.calls == n
    await load(board(8), store, call)
    assert call.calls == n


@pytest.mark.asyncio
async def test_model_calls_run_at_most_max_parallel_at_once(store):  # noqa: F811
    running = peak = 0

    async def call(prompt: str) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return "Ask the owner to decide today and record the answer on the board."

    await load(board(10), store, call)
    assert 1 < peak <= scrum_suggest.MAX_PARALLEL


# ── 4. cache lookups run together under one deadline ───────────────────────────────────────────

class SlowStore:
    def __init__(self, delay: float):
        self.delay = delay

    async def get_scrum_suggestion(self, *a):
        await asyncio.sleep(self.delay)
        return None

    async def save_scrum_suggestion(self, row):
        pass


@pytest.mark.asyncio
async def test_ten_lookups_of_a_third_of_a_second_do_not_add_up():
    t0 = time.monotonic()
    await scrum_suggest.attach_cached(board(10), SlowStore(0.3))
    assert time.monotonic() - t0 < 1.0                       # one after another would take 3 s


@pytest.mark.asyncio
async def test_lookups_past_the_deadline_serve_no_suggestion_and_cause_no_model_call(monkeypatch):
    monkeypatch.setattr(scrum_suggest, "LOOKUP_DEADLINE_SECS", 0.2)
    d, unresolved, call, st = board(3), set(), Counting(), SlowStore(5)
    t0 = time.monotonic()
    await scrum_suggest.attach_cached(d, st, unresolved=unresolved)
    assert time.monotonic() - t0 < 1.0
    assert all(e["suggestion"] is None for e in d["review_queue"]) and len(unresolved) == len(scrum_suggest._entries(d))
    scrum_suggest.refresh_async(d, st, call, skip=unresolved)
    await asyncio.gather(*list(scrum_suggest._TASKS))
    assert call.calls == 0                                   # a stored suggestion may exist; do not pay for another


# ── 5. the production store (rqlite) keeps suggestions ─────────────────────────────────────────

@pytest.fixture
def rq(tmp_path, monkeypatch):
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
    return r


@pytest.mark.asyncio
async def test_suggestions_persist_in_rqlite_and_the_next_load_makes_no_calls(rq):
    for stmt in rq.SCRUM_SUGGESTIONS_DDL:
        await rq._execute([[stmt]])
    call = Counting()
    await load(digest(), rq, call)
    n = call.calls
    assert n >= 1
    await load(digest(), rq, call)
    assert call.calls == n
    e = digest()["review_queue"][0]
    row = await rq.get_scrum_suggestion(e["item_key"], scrum_suggest.evidence_hash(e))
    assert row and (await rq.get_scrum_suggestion_by_id(row["id"]))["text"] == row["text"]
    await rq.save_scrum_suggestion({"id": row["id"], "item_key": e["item_key"], "evidence_hash": scrum_suggest.evidence_hash(e),
                                    "text": "a duplicate", "model": "x", "generated_at": "2026-11-04T00:00:00Z"})
    assert (await rq.get_scrum_suggestion(e["item_key"], scrum_suggest.evidence_hash(e)))["text"] == row["text"]   # first one kept


@pytest.mark.asyncio
async def test_concurrent_first_requests_on_a_fresh_sqlite_db_do_not_collide_on_the_schema_migration(tmp_path, monkeypatch):
    import importlib
    from orchestrator import db
    for n in range(8):                                       # the race needs the connections to interleave: try several fresh files
        monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / f"fresh{n}.db"))
        db = importlib.reload(db)
        rows = await asyncio.gather(*(db.get_scrum_suggestion(f"k{i}", "h") for i in range(30)))
        assert rows == [None] * 30


@pytest.mark.asyncio
async def test_a_board_larger_than_any_cap_is_fully_generated_in_one_run_and_the_next_load_makes_no_calls(store):  # noqa: F811
    call = Counting()
    n = len(scrum_suggest._entries(board(60)))
    await load(board(60), store, call)
    assert call.calls == n
    await load(board(60), store, call)
    assert call.calls == n


@pytest.mark.asyncio
async def test_a_failed_cache_read_is_not_a_miss_and_causes_no_model_call():
    class Broken:
        async def get_scrum_suggestion(self, *a):
            raise RuntimeError("rqlite no leader")

        async def save_scrum_suggestion(self, row):
            raise RuntimeError("rqlite no leader")

    d, unresolved, call, st = board(3), set(), Counting(), Broken()
    await scrum_suggest.attach_cached(d, st, unresolved=unresolved)
    assert len(unresolved) == len(scrum_suggest._entries(d)) and all(e["suggestion"] is None for e in d["review_queue"])
    scrum_suggest.refresh_async(d, st, call, skip=unresolved)
    await asyncio.gather(*list(scrum_suggest._TASKS))
    assert call.calls == 0

