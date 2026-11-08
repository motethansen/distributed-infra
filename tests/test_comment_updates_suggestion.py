"""DI-066: what the owner writes on the Vantage page changes the suggestion.

Michael, 2026-11-08: "in the vantage scrum-desk page, if I add a comment, the backend should take the comment into
consideration, and update suggestion". A comment is part of what a suggestion is made from: it changes the hash, so a new comment
produces a new suggestion with a new id (and an older confirm of the previous one is stale), the model is told what he wrote, and
until the new text is ready the board keeps showing the previous one, marked `updating`.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

from shared import scrum_digest as sd, scrum_suggest as ss
from tests.test_scrum_digest import TODAY, item, proj, sprint

KEY = {"X-Secret-Key": "k"}


def digest():
    return sd.build([proj("vantage", {"in_review": 1}, [item("VN-039", "in_review", "PR 3")], curve_last="2026-10-01",
                          sprints=[sprint(2, 8, 0)])], TODAY)


def entry(d, key="vantage:VN-039"):
    return next(e for e in d["review_queue"] if e["item_key"] == key)


def comment_on(e, text, cid=7):
    e["owner_comment"] = {"id": cid, "text": text, "at": "2026-11-08T03:00:00Z"}
    return e


class Recorder:
    def __init__(self):
        self.prompts = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        return "Ship it on Friday after the smoke test and record the result on the board."


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    from orchestrator import db
    return importlib.reload(db)


# ---- the hash and the prompt ------------------------------------------------------------------------------------

def test_an_item_without_a_comment_keeps_its_old_hash():
    e = entry(digest())
    assert ss.evidence_hash(e) == ss.base_hash(e)                       # nothing stored before this change is orphaned


def test_a_comment_changes_the_hash_and_so_the_suggestion_id():
    e = entry(digest())
    a, b = ss.evidence_hash(comment_on(dict(e), "wait for the legal answer")), ss.evidence_hash(comment_on(dict(e), "ship it now"))
    assert len({ss.base_hash(e), a, b}) == 3
    assert ss.suggestion_id(e["item_key"], a) != ss.suggestion_id(e["item_key"], ss.base_hash(e))


def test_the_comment_is_one_bounded_line_and_the_prompt_carries_it():
    e = comment_on(dict(entry(digest())), "  we should not\n gate the AI yet  " + "x" * 2000)
    assert "\n" not in ss.owner_comment(e) and len(ss.owner_comment(e)) <= ss.OWNER_COMMENT_MAX
    p = ss.prompt_for("review", e)
    assert "Owner's comment: we should not gate the AI yet" in p and "takes the comment into account" in p
    assert "Owner's comment" not in ss.prompt_for("review", entry(digest()))


# ---- generation and the previous suggestion --------------------------------------------------------------------

def test_a_comment_generates_a_new_suggestion_from_what_he_wrote(store):
    async def go():
        d = digest()
        rec = Recorder()
        await ss.attach_cached(d, store)
        ss.refresh_async(d, store, rec)
        await asyncio.sleep(0.2)
        old = ss.suggestion_id("vantage:VN-039", ss.base_hash(entry(d)))
        assert await store.get_scrum_suggestion("vantage:VN-039", ss.base_hash(entry(d))) and len(rec.prompts) >= 1
        n = len(rec.prompts)
        d2 = digest()
        comment_on(entry(d2), "legal terms first, do not ship the AI gate")
        await ss.attach_cached(d2, store)
        assert entry(d2)["suggestion"] is None                          # the new text does not exist yet
        assert await ss.attach_previous(d2, store) == 1
        shown = entry(d2)["suggestion"]
        assert shown["updating"] is True and shown["id"] == old         # the board keeps the previous one meanwhile
        ss.refresh_async(d2, store, rec)
        await asyncio.sleep(0.3)
        assert len(rec.prompts) == n + 1 and "legal terms first" in rec.prompts[-1]
        d3 = digest()
        comment_on(entry(d3), "legal terms first, do not ship the AI gate")
        await ss.attach_cached(d3, store)
        fresh = entry(d3)["suggestion"]
        assert fresh["id"] != old and "updating" not in fresh and fresh["text"]
        await ss.attach_previous(d3, store)
        assert "updating" not in entry(d3)["suggestion"]                # nothing to fall back to once the new one exists
    asyncio.run(go())


def test_the_same_comment_does_not_call_the_model_again(store):
    async def go():
        rec = Recorder()
        for _ in range(3):
            d = digest()
            comment_on(entry(d), "same words")
            await ss.attach_cached(d, store)
            await ss.attach_previous(d, store)
            ss.refresh_async(d, store, rec)
            await asyncio.sleep(0.3)
        return [p for p in rec.prompts if "Owner's comment: same words" in p]
    assert len(asyncio.run(go())) == 1                                  # generated once, then served from the store


def test_an_item_without_a_stored_previous_suggestion_stays_empty(store):
    async def go():
        d = digest()
        comment_on(entry(d), "words")
        await ss.attach_cached(d, store)
        assert await ss.attach_previous(d, store) == 0 and entry(d)["suggestion"] is None
    asyncio.run(go())


# ---- the orchestrator -------------------------------------------------------------------------------------------

@pytest.fixture
def mod(tmp_path, monkeypatch):
    from tests.test_scrum_responses import _load
    return _load(tmp_path, monkeypatch)


def respond(c, action, text="", item="VN-039", key="r1", sug="sg_1"):
    body = {"item_key": f"vantage:{item}", "project": "vantage", "item_id": item, "section": "review", "action": action,
            "suggestion_id": sug, "actor": "owner"}
    body.update({"suggestion_text": text} if action == "confirm" else {"comment": text})
    return c.post("/scrum/responses", json=body, headers={**KEY, "Idempotency-Key": key})


def test_attach_owner_comments_uses_the_newest_comment_per_item(mod):
    c = TestClient(mod.app)
    respond(c, "comment", "first thought", key="a")
    respond(c, "confirm", "A confirm is not a comment", key="b")
    respond(c, "comment", "second thought", key="c")
    respond(c, "comment", "about another item", item="VN-999", key="d")
    d = digest()
    asyncio.run(mod._attach_owner_comments(d))
    assert entry(d)["owner_comment"]["text"] == "second thought" and entry(d)["owner_comment"]["id"] > 0
    assert all("owner_comment" not in e for sec in ("decisions", "adjustments") for e in d[sec])


def test_a_comment_starts_the_regeneration_and_a_confirm_does_not(mod, monkeypatch):
    calls = []

    async def fake(secret):
        calls.append(secret)
    monkeypatch.setattr(mod, "_regenerate_after_comment", fake)
    c = TestClient(mod.app)
    r = respond(c, "comment", "legal terms first", key="x1")
    assert r.status_code == 201 and r.json()["applies"] == "suggestion_updating"
    assert calls == ["k"]
    r = respond(c, "confirm", "Product owner: check it", key="x2")
    assert r.status_code == 201 and calls == ["k"]


def test_the_regeneration_never_raises(mod, monkeypatch):
    async def boom(secret):
        raise RuntimeError("model down")
    monkeypatch.setattr(mod, "scrum_sprints", boom)
    asyncio.run(mod._regenerate_after_comment("k"))            # logs and returns


def test_turning_suggestions_off_turns_this_off(mod, monkeypatch):
    monkeypatch.setenv("SCRUM_SUGGESTIONS", "0")
    r = respond(TestClient(mod.app), "comment", "words", key="y1")
    assert r.status_code == 201 and "applies" not in r.json()
