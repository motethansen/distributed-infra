"""S-2: one Sonnet-written next step per board item, keyed by (item_key, evidence_hash)."""
from __future__ import annotations

import asyncio
import importlib
from datetime import date

import pytest

from shared import scrum_digest as sd, scrum_suggest
from tests.test_scrum import BACKLOG, SPRINT
from tests.test_scrum_digest import TODAY, item, proj, sprint


class Counting:
    """A mocked model client: counts calls and answers with one concrete step naming the item."""
    def __init__(self, fail=None):
        self.calls, self.fail = 0, fail

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        if self.fail == "raise":
            raise RuntimeError("model down")
        if self.fail == "hang":
            await asyncio.sleep(60)
        item = prompt.split("Item: ", 1)[1].splitlines()[0]
        return f"Ask the owner to decide on {item} today and record the answer on the board."


def digest():
    return sd.build([proj("vantage", {"in_review": 1, "blocked": 1}, [item("VN-039", "in_review", "PR 3"), item("VN-040", "blocked", "waiting on API key")],
                          curve_last="2026-10-01", sprints=[sprint(2, 8, 0)])], TODAY)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    from orchestrator import db
    return importlib.reload(db)


def all_entries(d):
    return d["review_queue"] + d["decisions"] + d["adjustments"]


def test_every_entry_has_a_stable_item_key_and_adjustments_are_objects_with_text():
    d = digest()
    assert d["review_queue"][0]["item_key"] == "vantage:VN-039"
    assert {x["item_key"] for x in d["decisions"]} == {"vantage:VN-040", "stall:vantage"}
    keys = {a["item_key"] for a in d["adjustments"]}
    assert "adjust:vantage:2:no_progress" in keys and "adjust:review_waiting" in keys
    assert all(isinstance(a, dict) and a["text"] and a["suggestion"] is None for a in d["adjustments"])
    assert sd.to_markdown(d).count("- vantage sprint 2: 0 of 8 points done") == 1          # still renders through .text
    assert sd.build([proj("vantage", sprints=[sprint(2, 10, 2, started="2026-10-01")])], TODAY)["adjustments"][0]["item_key"] == "adjust:vantage:2:stale_sprint"


async def load(d, store, call, **kw):
    """What the endpoint does: cached suggestions now, generation in the background; returns the digest as served."""
    await scrum_suggest.attach_cached(d, store)
    scrum_suggest.refresh_async(d, store, call)
    await asyncio.gather(*list(scrum_suggest._TASKS))
    return d


@pytest.mark.asyncio
async def test_first_load_serves_null_and_generates_in_the_background_then_cache_makes_no_calls(store):
    call = Counting()
    d1 = digest()
    await scrum_suggest.attach_cached(d1, store)
    scrum_suggest.refresh_async(d1, store, call)
    assert all(e["suggestion"] is None for e in all_entries(d1)) and call.calls == 0     # nothing waited on the request path
    await asyncio.gather(*list(scrum_suggest._TASKS))
    n = len(all_entries(d1))
    assert call.calls == n
    d2 = await load(digest(), store, call)
    s = d2["review_queue"][0]["suggestion"]
    assert set(s) == {"id", "text", "generated_at", "model"} and s["model"] == "sonnet" and all(e["suggestion"] for e in all_entries(d2))
    d3 = await load(digest(), store, call)
    assert call.calls == n and d3["review_queue"][0]["suggestion"] == s


@pytest.mark.asyncio
async def test_changed_evidence_gives_a_new_id_and_the_old_row_stays_readable(store):
    call = Counting()
    await load(digest(), store, call)
    old = (await load(digest(), store, call))["review_queue"][0]["suggestion"]
    changed = sd.build([proj("vantage", {"in_review": 1}, [item("VN-039", "in_review", "PR 3 merged, awaiting QA")])], TODAY)
    await load(changed, store, call)
    new = (await load(sd.build([proj("vantage", {"in_review": 1}, [item("VN-039", "in_review", "PR 3 merged, awaiting QA")])], TODAY), store, call))["review_queue"][0]["suggestion"]
    assert new["id"] != old["id"]
    kept = await store.get_scrum_suggestion_by_id(old["id"])
    assert kept["text"] == old["text"] and kept["item_key"] == "vantage:VN-039"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "hang"])
async def test_a_failing_or_slow_model_leaves_null_suggestions_and_the_board_loads(store, mode, monkeypatch):
    monkeypatch.setattr(scrum_suggest, "CALL_TIMEOUT_SECS", 0.2)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    d = await load(digest(), store, Counting(fail=mode))
    assert loop.time() - t0 < 2 and all(e["suggestion"] is None for e in all_entries(d))
    assert await store.get_scrum_suggestion("vantage:VN-039", scrum_suggest.evidence_hash(d["review_queue"][0])) is None
    assert not scrum_suggest._IN_FLIGHT


@pytest.mark.asyncio
async def test_a_failing_store_does_not_break_the_board():
    class Broken:
        async def get_scrum_suggestion(self, *a): raise RuntimeError("rqlite no leader")
        async def save_scrum_suggestion(self, row): raise RuntimeError("rqlite no leader")
    d = await load(digest(), Broken(), Counting())
    assert all(e["suggestion"] is None for e in all_entries(d))


@pytest.mark.asyncio
async def test_each_suggestion_is_at_most_300_chars_names_one_step_and_is_one_line(store):
    async def rambling(prompt):          # a model that ignores the length rule: it is clamped, not stored raw
        return "Decide on VN-039 now. " + "and then also think about everything else " * 20 + "\nsecond line"
    await load(digest(), store, rambling)
    d = await load(digest(), store, rambling)
    for e in all_entries(d):
        t = e["suggestion"]["text"]
        assert 0 < len(t) <= 300 and "\n" not in t
    one = lambda: sd.build([proj("vantage", {"in_review": 1}, [item("VN-050", "in_review")])], TODAY)
    await load(one(), store, Counting())
    t = (await load(one(), store, Counting()))["review_queue"][0]["suggestion"]["text"]
    assert len(t) <= 300 and "VN-050" in t and t.split()[0].lower() == "ask"        # an action addressed to someone, naming the item
    assert "ONE concrete next step" in scrum_suggest.PROMPT and "at most 250 characters" in scrum_suggest.PROMPT


def test_clean_rejects_empty_and_clamps_long_text():
    assert scrum_suggest.clean("  \n ") is None and scrum_suggest.clean(None) is None
    assert len(scrum_suggest.clean("word " * 200)) <= 300


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db")); monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "k")
    repo = tmp_path / "repo"; (repo / "demo").mkdir(parents=True)
    (repo / "demo" / "product-backlog.md").write_text(BACKLOG); (repo / "demo" / "sprint-02.md").write_text(SPRINT)
    monkeypatch.setenv("SCRUM_REPO", str(repo))
    import orchestrator.db as dbm; importlib.reload(dbm)
    import orchestrator.main as m; importlib.reload(m)
    return m


@pytest.mark.asyncio
async def test_endpoint_serves_null_first_then_cached_and_unchanged_backlogs_make_zero_calls(api, monkeypatch):
    call = Counting()
    monkeypatch.setattr(api, "_suggest_call", call)
    first = (await api.scrum_sprints(x_secret_key="k"))["digest"]
    entries = all_entries(first)
    assert entries and all("item_key" in e and e["suggestion"] is None for e in entries)
    await asyncio.gather(*list(scrum_suggest._TASKS))
    assert call.calls == len(entries)
    n = call.calls
    second = (await api.scrum_sprints(x_secret_key="k"))["digest"]
    assert all(e["suggestion"] and "item_key" in e for e in all_entries(second)) and len(all_entries(second)) == n
    assert call.calls == n
    third = (await api.scrum_sprints(x_secret_key="k"))["digest"]
    assert call.calls == n and [e["suggestion"] for e in all_entries(third)] == [e["suggestion"] for e in all_entries(second)]
