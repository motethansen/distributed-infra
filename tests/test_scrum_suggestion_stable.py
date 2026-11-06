"""Suggestions stay stable when the store is slow or down: one batch query, and a last-known-good row per evidence hash."""
from __future__ import annotations

import asyncio
import importlib
import sqlite3
import time

import pytest
import pytest_asyncio

from shared import scrum_suggest as ss
from tests.test_scrum_suggest import digest, all_entries


def row(item_key, h, text="Do the thing."):
    return {"id": ss.suggestion_id(item_key, h), "item_key": item_key, "evidence_hash": h, "text": text,
            "model": "sonnet", "generated_at": "2026-11-01T00:00:00Z"}


@pytest.fixture(autouse=True)
def clean_known():
    getattr(ss, "_KNOWN", {}).clear()
    yield
    getattr(ss, "_KNOWN", {}).clear()


@pytest.fixture
def sqlite_store(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    from orchestrator import db
    return importlib.reload(db)


@pytest_asyncio.fixture
async def rqlite_store(tmp_path, monkeypatch):
    import orchestrator.db_rqlite as dbrq
    conn = sqlite3.connect(str(tmp_path / "rq.db"))

    async def fake_post(path, statements, params=None):
        out = []
        for sql, *args in statements:
            cur = conn.execute(sql, args)
            if cur.description:
                out.append({"columns": [d[0] for d in cur.description], "values": [list(x) for x in cur.fetchall()]})
            else:
                out.append({"last_insert_id": cur.lastrowid, "rows_affected": cur.rowcount})
        conn.commit()
        fake_post.calls += 1
        return out
    fake_post.calls = 0
    monkeypatch.setattr(dbrq, "_post", fake_post)
    await dbrq.ensure_schema()
    dbrq._fake_post = fake_post
    yield dbrq
    conn.close()


async def seed(store, d):
    for e in all_entries(d):
        await store.save_scrum_suggestion(row(e["item_key"], ss.evidence_hash(e), f"Step for {e['item_key']}"))


@pytest_asyncio.fixture(params=["sqlite", "rqlite"])
async def store(request, sqlite_store, rqlite_store):
    return sqlite_store if request.param == "sqlite" else rqlite_store


@pytest.mark.asyncio
async def test_batch_lookup_matches_per_item_path(store):
    d = digest()
    await seed(store, d)
    pairs = [(e["item_key"], ss.evidence_hash(e)) for e in all_entries(d)] + [("nope:1", "deadbeef"), ("vantage:VN-039", "otherhash")]
    batch = await store.get_scrum_suggestions(pairs)
    single = {p: await store.get_scrum_suggestion(*p) for p in pairs}
    assert batch == {p: r for p, r in single.items() if r}
    assert ("nope:1", "deadbeef") not in batch and ("vantage:VN-039", "otherhash") not in batch
    assert await store.get_scrum_suggestions([]) == {}


@pytest.mark.asyncio
async def test_batch_chunks_under_the_variable_limit(sqlite_store):
    pairs = [(f"k{i}", "h") for i in range(1500)]
    await sqlite_store.save_scrum_suggestion(row("k1499", "h"))
    got = await sqlite_store.get_scrum_suggestions(pairs)
    assert list(got) == [("k1499", "h")]


@pytest.mark.asyncio
async def test_rqlite_batch_is_one_round_trip(rqlite_store):
    d = digest()
    await seed(rqlite_store, d)
    before = rqlite_store._fake_post.calls
    await rqlite_store.get_scrum_suggestions([(e["item_key"], ss.evidence_hash(e)) for e in all_entries(d)])
    assert rqlite_store._fake_post.calls - before == 1


class Flaky:
    """Per-item-only store (no batch method) that can go slow or fail after a good load."""
    def __init__(self, rows):
        self.rows, self.mode = rows, "ok"

    async def get_scrum_suggestion(self, k, h):
        if self.mode == "slow":
            await asyncio.sleep(60)
        if self.mode == "raise":
            raise RuntimeError("rqlite down")
        return self.rows.get((k, h))


class FlakyBatch(Flaky):
    async def get_scrum_suggestions(self, pairs):
        if self.mode == "slow":
            await asyncio.sleep(60)
        if self.mode == "raise":
            raise RuntimeError("rqlite down")
        return {p: self.rows[p] for p in pairs if p in self.rows}


def rows_for(d):
    return {(e["item_key"], ss.evidence_hash(e)): row(e["item_key"], ss.evidence_hash(e)) for e in all_entries(d)}


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [Flaky, FlakyBatch])
@pytest.mark.parametrize("mode", ["slow", "raise"])
async def test_ids_stay_stable_when_store_fails_after_a_good_load(cls, mode, monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.2)
    d1 = digest()
    store = cls(rows_for(d1))
    await ss.attach_cached(d1, store)
    ids = [e["suggestion"]["id"] for e in all_entries(d1)]
    assert all(ids)
    store.mode = mode
    d2, unresolved = digest(), set()
    await ss.attach_cached(d2, store, unresolved=unresolved)
    assert [e["suggestion"]["id"] for e in all_entries(d2)] == ids
    assert unresolved == set()


@pytest.mark.asyncio
async def test_changed_hash_does_not_reuse_old_row_and_never_seen_stays_unresolved(monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.2)
    d1 = digest()
    store = FlakyBatch(rows_for(d1))
    await ss.attach_cached(d1, store)
    store.mode = "raise"
    d2, unresolved = digest(), set()
    target = d2["review_queue"][0]
    target["reason"] = "totally different evidence"
    assert ss.evidence_hash(target) != ss.evidence_hash(d1["review_queue"][0])
    await ss.attach_cached(d2, store, unresolved=unresolved)
    assert target["suggestion"] is None and target["item_key"] in unresolved       # no old row for a new hash
    others = [e for e in all_entries(d2) if e is not target and e["item_key"] != target["item_key"]]
    assert others and all(e["suggestion"] for e in others)


@pytest.mark.asyncio
async def test_never_seen_items_stay_null_and_unresolved_without_model_calls(monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.2)
    d = digest()
    store = FlakyBatch({})
    store.mode = "raise"
    unresolved: set[str] = set()
    await ss.attach_cached(d, store, unresolved=unresolved)
    assert all(e["suggestion"] is None for e in all_entries(d))
    assert unresolved == {e["item_key"] for e in all_entries(d)}
    calls = []

    async def call(prompt):
        calls.append(prompt)
        return "x"
    ss.refresh_async(d, store, call, skip=unresolved)
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_deadline_is_a_hard_bound_and_logs_one_line(monkeypatch, caplog):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.3)
    d = digest()
    store = Flaky({})
    store.mode = "slow"
    t0 = time.monotonic()
    with caplog.at_level("WARNING", logger=ss.log.name):
        await ss.attach_cached(d, store)
    assert time.monotonic() - t0 < 1.5
    assert len([r for r in caplog.records if "lookup" in r.getMessage()]) == 1


def test_deadline_constant_and_lru_bound():
    assert ss.LOOKUP_DEADLINE_SECS == 4.0
    for i in range(ss.KNOWN_MAX + 50):
        ss.remember(f"k{i}", "h", row(f"k{i}", "h"))
    assert len(ss._KNOWN) == ss.KNOWN_MAX and ("k0", "h") not in ss._KNOWN and (f"k{ss.KNOWN_MAX + 49}", "h") in ss._KNOWN


class Stubborn:
    """Store that ignores cancellation: it swallows CancelledError and, after `delay`, returns a (late) row."""
    def __init__(self, rows, batch):
        self.rows, self.delay, self.hang = rows, 0.6, False
        if batch:
            self.get_scrum_suggestions = self._batch

    async def _wait(self):
        if not self.hang:
            return
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(self.delay)               # slow to cancel, then answers anyway

    async def get_scrum_suggestion(self, k, h):
        await self._wait()
        return self.rows.get((k, h))

    async def _batch(self, pairs):
        await self._wait()
        return {p: self.rows[p] for p in pairs if p in self.rows}


async def drain():
    await asyncio.gather(*list(ss._STRAGGLERS.values()), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [Flaky, FlakyBatch])
@pytest.mark.parametrize("mode", ["slow", "raise"])
async def test_confirmed_miss_evicts_cache_so_later_failure_does_not_resurrect(cls, mode, monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.2)
    d1 = digest()
    store = cls(rows_for(d1))
    await ss.attach_cached(d1, store)
    assert all(e["suggestion"] for e in all_entries(d1))
    store.rows.clear()                                    # row deleted; the lookup succeeds and returns no row
    d2 = digest()
    await ss.attach_cached(d2, store)
    assert all(e["suggestion"] is None for e in all_entries(d2)) and not ss._KNOWN
    store.mode = mode
    d3, unresolved = digest(), set()
    await ss.attach_cached(d3, store, unresolved=unresolved)
    assert all(e["suggestion"] is None for e in all_entries(d3))
    assert unresolved == {e["item_key"] for e in all_entries(d3)}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [True, False])
async def test_deadline_holds_when_store_delays_cancellation_and_late_row_is_ignored(batch, monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.2)
    d = digest()
    store = Stubborn(rows_for(d), batch)
    store.hang = True
    unresolved: set[str] = set()
    t0 = time.monotonic()
    await ss.attach_cached(d, store, unresolved=unresolved)
    assert time.monotonic() - t0 < 0.5                    # not 0.2 + 0.6: cancellation is not awaited
    assert all(e["suggestion"] is None for e in all_entries(d))
    assert unresolved == {e["item_key"] for e in all_entries(d)}
    await drain()                                         # the late rows have now been returned by the store
    assert all(e["suggestion"] is None for e in all_entries(d)) and not ss._KNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [True, False])
async def test_late_row_does_not_replace_cache_or_digest(batch, monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.2)
    d1 = digest()
    rows = rows_for(d1)
    store = Stubborn(rows, batch)
    await ss.attach_cached(d1, store)
    before = {k: dict(v) for k, v in ((k, v["row"]) for k, v in ss._KNOWN.items())}
    for k in rows:
        rows[k] = {**rows[k], "text": "LATE", "id": "sg_late"}
    store.hang = True
    d2 = digest()
    await ss.attach_cached(d2, store)
    await drain()
    assert {k: dict(v["row"]) for k, v in ss._KNOWN.items()} == before
    assert all(e["suggestion"]["text"] != "LATE" for e in all_entries(d2))


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [Flaky, FlakyBatch])
async def test_fallback_rows_are_marked_stale_with_verified_at_and_fresh_rows_are_not(cls, monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.2)
    d1 = digest()
    store = cls(rows_for(d1))
    await ss.attach_cached(d1, store)
    for e in all_entries(d1):
        assert set(e["suggestion"]) == {"id", "text", "generated_at", "model"}
    store.mode = "raise"
    d2 = digest()
    await ss.attach_cached(d2, store)
    for e1, e2 in zip(all_entries(d1), all_entries(d2)):
        s = e2["suggestion"]
        assert s["stale"] is True and s["verified_at"] >= "2026" and s["verified_at"].endswith("Z")
        assert {k: v for k, v in s.items() if k not in ("stale", "verified_at")} == e1["suggestion"]
        assert not e1["suggestion"].get("stale")
    store.mode = "ok"
    d3 = digest()
    await ss.attach_cached(d3, store)
    assert all("stale" not in e["suggestion"] and "verified_at" not in e["suggestion"] for e in all_entries(d3))


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [True, False])
async def test_cancelling_the_parent_cancels_and_reaps_children(batch, monkeypatch, recwarn):
    d = digest()
    store = Stubborn(rows_for(d), batch)
    store.hang = True
    parent = asyncio.ensure_future(ss.attach_cached(d, store))
    await asyncio.sleep(0.05)
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert ss._STRAGGLERS and all(t.cancelling() for t in ss._STRAGGLERS.values())
    await drain()
    await asyncio.sleep(0)
    assert not ss._STRAGGLERS                             # every child ran its reaper


@pytest.mark.asyncio
async def test_cancelled_parent_retrieves_a_child_exception(monkeypatch, caplog):
    class Boom:
        async def get_scrum_suggestion(self, k, h):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise RuntimeError("late failure")
    parent = asyncio.ensure_future(ss.attach_cached(digest(), Boom()))
    await asyncio.sleep(0.05)
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    await drain()
    await asyncio.sleep(0)
    import gc
    gc.collect()
    assert "never retrieved" not in caplog.text and not ss._STRAGGLERS


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [True, False])
async def test_result_finished_after_the_deadline_is_ignored_even_if_the_loop_was_late(batch, monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.05)
    d1 = digest()
    rows = rows_for(d1)

    class Blocking:
        def __init__(self):
            if batch:
                self.get_scrum_suggestions = self._batch

        async def get_scrum_suggestion(self, k, h):
            time.sleep(0.2)                               # blocks the loop: finishes after the deadline, before wait returns
            return rows.get((k, h))

        async def _batch(self, pairs):
            time.sleep(0.2)
            return {p: rows[p] for p in pairs if p in rows}

    unresolved: set[str] = set()
    await ss.attach_cached(d1, Blocking(), unresolved=unresolved)
    assert all(e["suggestion"] is None for e in all_entries(d1)) and not ss._KNOWN
    assert unresolved == {e["item_key"] for e in all_entries(d1)}


@pytest.mark.asyncio
async def test_cap_stops_new_lookups_and_falls_back_like_a_timeout(monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.2)
    d1 = digest()
    store = Stubborn(rows_for(d1), False)
    await ss.attach_cached(d1, store)                     # seeds last-known rows
    monkeypatch.setattr(ss, "MAX_OUTSTANDING", 2)
    started = []
    orig = store.get_scrum_suggestion

    async def counting(k, h):
        started.append((k, h))
        return await orig(k, h)
    store.get_scrum_suggestion = counting
    store.hang = True
    d2, unresolved = digest(), set()
    await ss.attach_cached(d2, store, unresolved=unresolved)
    n = len(all_entries(d2))
    assert n > 2 and len(started) == 2 and len(ss._STRAGGLERS) <= 2
    assert all(e["suggestion"]["stale"] for e in all_entries(d2))     # skipped and timed out alike: stale fallback
    await drain()


@pytest.mark.asyncio
async def test_no_second_lookup_for_a_key_already_outstanding(monkeypatch):
    monkeypatch.setattr(ss, "LOOKUP_DEADLINE_SECS", 0.1)
    d1 = digest()
    store = Stubborn(rows_for(d1), False)
    store.hang = True
    started = []
    orig = store.get_scrum_suggestion

    async def counting(k, h):
        started.append((k, h))
        return await orig(k, h)
    store.get_scrum_suggestion = counting
    await ss.attach_cached(d1, store)
    first = len(started)
    assert first and len(ss._STRAGGLERS) == first
    d2, unresolved = digest(), set()
    await ss.attach_cached(d2, store, unresolved=unresolved)
    assert len(started) == first                          # the stragglers are still running: nothing new started
    assert unresolved == {e["item_key"] for e in all_entries(d2)}
    await drain()
