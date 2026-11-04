"""Independent acceptance tests for story S-2: Scrum Master suggests one action per review item, decision and adjustment, with a stable key."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from shared import scrum_digest as sd, scrum_next as sn, scrum_suggest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "clients" / "fleet-mcp")]
import dis_agent as da  # noqa: E402
import scrum_cli as sc  # noqa: E402

TODAY = date(2026, 10, 31)

FIXTURE_BACKLOG_V1 = """---
project: vantage
---
# Vantage backlog

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| VN-039 | Ship payment webhook | High | 🔍 in review | PR #12 green |
| VN-040 | Configure production webhook secret | High | 🚧 blocked | waiting on Stripe dashboard secret |
| VN-041 | Add retry backoff | Medium | 🔵 planned | |
"""

FIXTURE_BACKLOG_V2 = """---
project: vantage
---
# Vantage backlog

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| VN-039 | Ship payment webhook | High | 🔍 in review | PR #12 merged, staging verified |
| VN-040 | Configure production webhook secret | High | 🚧 blocked | waiting on Stripe dashboard secret |
| VN-041 | Add retry backoff | Medium | 🔵 planned | |
"""

FIXTURE_SPRINT = """---
project: vantage
sprint: 02
started: 2026-10-01
goal: Payment reliability
status: open
---
# Vantage sprint 2

| Story | From backlog | Assignee | Points | Note |
|---|---|---|---|---|
| **VN-039** Ship payment webhook 🔍 | product-backlog | mac-mini | 3 | PR #12 |
| **VN-040** Configure production webhook secret 🚧 | product-backlog | thinkpad | 5 | blocked |
| **VN-041** Add retry backoff ✅ | product-backlog | mac-mini | 2 | done in sprint |

| Date | Remaining points | Note |
|---|---|---|
| 2026-10-01 | 10 | opened |
| 2026-10-05 | 8 | VN-041 |
"""


def _proj(name="vantage", counts=None, items=(), curve_last=None, sprints=(), velocity=(), closed=3, stories=10):
    c = {"done": 0, "superseded": 0, "in_progress": 0, "in_review": 0, "blocked": 0, "planned": 0}
    c.update(counts or {})
    return {
        "project": name,
        "counts": c,
        "closed": closed,
        "stories": stories,
        "complete_pct": 30.0,
        "active_items": list(items),
        "scurve": {"curve": [{"date": curve_last}] if curve_last else []},
        "sprints": list(sprints),
        "velocity": list(velocity),
    }


def _item(i, st, ev="e"):
    return {"id": i, "status": st, "title": f"title {i}", "evidence": ev}


def _sprint(n, total, done, started="2026-10-30", drift=()):
    return {
        "sprint": n,
        "points_total": total,
        "points_done": done,
        "points_remaining": total - done,
        "started": started,
        "updated": started,
        "drift": list(drift),
    }


class CountingModelMock:
    """Mocked Sonnet client counting calls and returning a concrete next step."""

    def __init__(self, fail_mode: str | None = None, custom_reply: str | None = None):
        self.calls = 0
        self.prompts: list[str] = []
        self.fail_mode = fail_mode
        self.custom_reply = custom_reply

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        if self.fail_mode == "raise":
            raise RuntimeError("mock model failure")
        if self.fail_mode == "timeout":
            await asyncio.sleep(30)
        if self.custom_reply is not None:
            return self.custom_reply
        item_key = "item"
        if "Item: " in prompt:
            item_key = prompt.split("Item: ", 1)[1].splitlines()[0].strip()
        return f"Review and confirm {item_key} with the owner in the dashboard today."


@pytest.fixture
def s2_env(tmp_path, monkeypatch):
    db_path = tmp_path / "s2_test.db"
    repo = tmp_path / "scrum_repo"
    (repo / "vantage").mkdir(parents=True)
    (repo / "vantage" / "product-backlog.md").write_text(FIXTURE_BACKLOG_V1)
    (repo / "vantage" / "sprint-02.md").write_text(FIXTURE_SPRINT)

    monkeypatch.setenv("QUEUE_DB_PATH", str(db_path))
    monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "s2-secret")
    monkeypatch.setenv("SCRUM_REPO", str(repo))
    monkeypatch.delenv("SCRUM_SUGGESTIONS", raising=False)

    import orchestrator.db as dbm
    import orchestrator.main as m

    importlib.reload(dbm)
    importlib.reload(m)
    return m, dbm, repo, db_path


async def _call_sprints(m, wait_tasks: bool = True) -> dict:
    resp = await m.scrum_sprints(x_secret_key="s2-secret")
    if wait_tasks and scrum_suggest._TASKS:
        await asyncio.gather(*list(scrum_suggest._TASKS))
    return resp


def _all_digest_entries(digest: dict) -> list[dict]:
    return list(digest["review_queue"]) + list(digest["decisions"]) + list(digest["adjustments"])


# ── Criterion 1 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_c1_sprints_digest_entries_carry_stable_item_key_and_suggestion_or_null(s2_env, monkeypatch):
    m, _dbm, _repo, _db_path = s2_env
    mock_model = CountingModelMock()
    monkeypatch.setattr(m, "_suggest_call", mock_model)

    # Unit check on digest structure for specific item_key formats mentioned in AC 1
    d = sd.build(
        [
            _proj(
                "vantage",
                {"in_review": 1, "blocked": 1, "planned": 2},
                [_item("VN-039", "in_review", "PR #12"), _item("VN-040", "blocked", "needs key")],
                curve_last="2026-10-01",
                sprints=[_sprint(2, 10, 2, started="2026-10-01")],
            )
        ],
        TODAY,
    )
    assert d["review_queue"][0]["item_key"] == "vantage:VN-039"
    decision_keys = {x["item_key"] for x in d["decisions"]}
    assert "vantage:VN-040" in decision_keys
    assert "stall:vantage" in decision_keys
    adjustment_keys = {a["item_key"] for a in d["adjustments"]}
    assert "adjust:vantage:2:stale_sprint" in adjustment_keys

    for entry in _all_digest_entries(d):
        assert isinstance(entry, dict)
        assert isinstance(entry.get("item_key"), str) and entry["item_key"]
        assert "suggestion" in entry and entry["suggestion"] is None
    for adj in d["adjustments"]:
        assert isinstance(adj, dict) and not isinstance(adj, str)
        assert isinstance(adj.get("text"), str) and adj["text"]

    # Endpoint check via GET /scrum/sprints
    first_resp = await _call_sprints(m, wait_tasks=True)
    first_entries = _all_digest_entries(first_resp["digest"])
    assert len(first_entries) > 0
    for entry in first_entries:
        assert isinstance(entry, dict)
        assert isinstance(entry.get("item_key"), str) and entry["item_key"]
        assert entry["suggestion"] is None

    second_resp = await _call_sprints(m, wait_tasks=True)
    second_entries = _all_digest_entries(second_resp["digest"])
    for entry in second_entries:
        assert isinstance(entry, dict)
        assert isinstance(entry.get("item_key"), str) and entry["item_key"]
        sug = entry["suggestion"]
        assert isinstance(sug, dict)
        assert set(sug.keys()) == {"id", "text", "generated_at", "model"}
        assert isinstance(sug["id"], str) and sug["id"]
        assert isinstance(sug["text"], str) and sug["text"]
        assert isinstance(sug["generated_at"], str) and sug["generated_at"]
        assert sug["model"] == "sonnet"


# ── Criterion 2 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_c2_sonnet_prompt_in_shared_stored_in_scrum_suggestions_and_second_call_makes_zero_model_calls(
    s2_env, tmp_path, monkeypatch
):
    m, _dbm, _repo, db_path = s2_env
    mock_model = CountingModelMock()
    monkeypatch.setattr(m, "_suggest_call", mock_model)

    # Prompt lives in shared/ and model is sonnet
    assert hasattr(scrum_suggest, "PROMPT") and isinstance(scrum_suggest.PROMPT, str) and len(scrum_suggest.PROMPT) > 20
    assert Path(scrum_suggest.__file__).resolve().parent.name == "shared"

    # First /scrum/sprints call triggers generation for uncached items
    resp1 = await _call_sprints(m, wait_tasks=True)
    entries1 = _all_digest_entries(resp1["digest"])
    assert len(entries1) > 0

    calls_after_first = mock_model.calls
    assert calls_after_first == len(entries1)

    # Verify rows are stored in scrum_suggestions table keyed by (item_key, evidence_hash)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT item_key, evidence_hash, id, text, model, generated_at FROM scrum_suggestions"
        ).fetchall()
        assert len(rows) == len(entries1)
        stored_keys = {(r[0], r[1]) for r in rows}
        for e in entries1:
            assert (e["item_key"], scrum_suggest.evidence_hash(e)) in stored_keys
    finally:
        conn.close()

    # Verify rqlite scrum_suggestions table DDL & operations keyed by (item_key, evidence_hash)
    import orchestrator.db_rqlite as dbrq

    rq_conn = sqlite3.connect(str(tmp_path / "rq_s2.db"))
    try:
        async def fake_rq_post(path, statements, params=None):
            out = []
            for sql, *args in statements:
                cur = rq_conn.execute(sql, args)
                if cur.description:
                    out.append({"columns": [d[0] for d in cur.description], "values": [list(x) for x in cur.fetchall()]})
                else:
                    out.append({"last_insert_id": cur.lastrowid, "rows_affected": cur.rowcount})
            rq_conn.commit()
            return out

        monkeypatch.setattr(dbrq, "_post", fake_rq_post)
        await dbrq.ensure_schema()

        rq_mock = CountingModelMock()
        monkeypatch.setattr(m, "_suggest_call", rq_mock)
        monkeypatch.setattr(m, "db", dbrq)

        await _call_sprints(m, wait_tasks=True)
        assert rq_mock.calls == len(entries1)
        rq_resp2 = await _call_sprints(m, wait_tasks=True)
        assert rq_mock.calls == len(entries1)  # 0 model calls on second load with rqlite backend
        assert all(e["suggestion"] is not None for e in _all_digest_entries(rq_resp2["digest"]))
    finally:
        rq_conn.close()
        monkeypatch.setattr(m, "db", _dbm)
        monkeypatch.setattr(m, "_suggest_call", mock_model)

    # Second /scrum/sprints call with unchanged backlogs makes 0 model calls
    resp2 = await _call_sprints(m, wait_tasks=True)
    assert mock_model.calls == calls_after_first
    assert all(e["suggestion"] is not None for e in _all_digest_entries(resp2["digest"]))


# ── Criterion 3 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_c3_changing_backlog_evidence_produces_new_suggestion_id_and_keeps_old_row_readable(
    s2_env, monkeypatch
):
    m, dbm, repo, _db_path = s2_env
    mock_model = CountingModelMock()
    monkeypatch.setattr(m, "_suggest_call", mock_model)

    # Load v1 backlog and populate cache
    await _call_sprints(m, wait_tasks=True)
    digest_v1 = (await _call_sprints(m, wait_tasks=True))["digest"]
    vn039_v1 = next(r for r in digest_v1["review_queue"] if r["item_key"] == "vantage:VN-039")
    old_suggestion = vn039_v1["suggestion"]
    assert old_suggestion is not None

    # Update VN-039 evidence in fixture backlog and expire file cache
    (repo / "vantage" / "product-backlog.md").write_text(FIXTURE_BACKLOG_V2)
    m._SCRUM_CACHE["ts"] = 0

    await _call_sprints(m, wait_tasks=True)
    digest_v2 = (await _call_sprints(m, wait_tasks=True))["digest"]
    vn039_v2 = next(r for r in digest_v2["review_queue"] if r["item_key"] == "vantage:VN-039")
    new_suggestion = vn039_v2["suggestion"]
    assert new_suggestion is not None
    assert new_suggestion["id"] != old_suggestion["id"]

    # Old row stays readable by suggestion_id
    old_row = await dbm.get_scrum_suggestion_by_id(old_suggestion["id"])
    assert old_row is not None
    assert old_row["id"] == old_suggestion["id"]
    assert old_row["item_key"] == "vantage:VN-039"
    assert old_row["text"] == old_suggestion["text"]


# ── Criterion 4 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_mode", ["raise", "timeout"])
async def test_c4_failing_or_timing_out_model_leaves_suggestion_null_and_loads_under_timeout(
    s2_env, monkeypatch, fail_mode
):
    m, _dbm, _repo, _db_path = s2_env
    monkeypatch.setattr(scrum_suggest, "CALL_TIMEOUT_SECS", 0.15)
    failing_mock = CountingModelMock(fail_mode=fail_mode)
    monkeypatch.setattr(m, "_suggest_call", failing_mock)

    t0 = time.monotonic()
    resp = await m.scrum_sprints(x_secret_key="s2-secret")
    elapsed_request = time.monotonic() - t0
    assert elapsed_request < 1.0

    entries = _all_digest_entries(resp["digest"])
    assert len(entries) > 0
    assert all(e["suggestion"] is None for e in entries)

    if scrum_suggest._TASKS:
        await asyncio.gather(*list(scrum_suggest._TASKS))
    total_elapsed = time.monotonic() - t0
    assert total_elapsed < 2.0

    resp_after = await _call_sprints(m, wait_tasks=True)
    assert all(e["suggestion"] is None for e in _all_digest_entries(resp_after["digest"]))


# ── Criterion 5 ──────────────────────────────────────────────────────────────


def test_c5_existing_digest_consumers_to_markdown_and_dis_agent_scrum_next_review_work_with_object_adjustments(
    tmp_path, monkeypatch, capsys
):
    d = sd.build(
        [
            _proj(
                "vantage",
                {"in_review": 1, "blocked": 1, "planned": 2},
                [_item("VN-039", "in_review", "PR #12 green"), _item("VN-040", "blocked", "waiting on Stripe")],
                curve_last="2026-10-01",
                sprints=[_sprint(2, 8, 0, started="2026-10-15")],
            )
        ],
        TODAY,
        {"spent": 42.0, "cap": 50.0},
    )

    # Adjustments have .text field ("text" key on adjustment dict) and render in to_markdown
    assert len(d["adjustments"]) > 0
    for adj in d["adjustments"]:
        assert isinstance(adj, dict)
        assert isinstance(adj["text"], str) and len(adj["text"]) > 0

    md = sd.to_markdown(d, fleet="25 tasks")
    for adj in d["adjustments"]:
        assert f"- {adj['text']}" in md

    # dis_agent next consumer (scrum_next.suggest + render)
    board = {
        "fleet": {"totals": {"tasks": 25, "done": 20, "tokens_out": 5000}},
        "projects": [
            {
                "project": "vantage",
                "backlog_view": {
                    "sprints": [{"sprint": "2", "points_remaining": 5, "stories": [{"id": "VN-039", "status": "in_review"}]}],
                    "unscheduled": [{"id": "VN-040", "status": "blocked", "evidence": ""}],
                    "cancelled": [],
                },
            }
        ],
        "digest": d,
    }
    suggestions = sn.suggest(board, [], {})
    rendered_next = sn.render(suggestions)
    assert "VN-039" in rendered_next or "/review" in rendered_next

    # dis_agent scrum & review CLI commands
    monkeypatch.setattr(da, "ROOT", tmp_path)
    monkeypatch.setattr(da, "projects_cfg", lambda: {"projects": {"vantage": {"repo": "git@github.com:o/vantage.git"}}})
    monkeypatch.setattr(sc, "client", lambda: type("C", (), {"_call": lambda self, *a, **k: board})())

    da.cmd_scrum(argparse.Namespace(narrate=False, as_agent=None))
    scrum_out = capsys.readouterr().out
    assert "Ready for your review (1)" in scrum_out
    assert "VN-039" in scrum_out
    assert any(adj["text"] in scrum_out for adj in d["adjustments"])

    da.cmd_review(argparse.Namespace(project=None, sprint=None))
    review_out = capsys.readouterr().out
    assert "1 stories wait for your sprint review" in review_out
    assert "VN-039" in review_out


# ── Criterion 6 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_c6_each_suggestion_text_is_at_most_300_chars_and_names_one_concrete_next_step(s2_env, monkeypatch):
    m, _dbm, _repo, _db_path = s2_env

    # 1) Normal mock returning a concrete step per item
    mock_model = CountingModelMock()
    monkeypatch.setattr(m, "_suggest_call", mock_model)
    await _call_sprints(m, wait_tasks=True)
    digest = (await _call_sprints(m, wait_tasks=True))["digest"]
    entries = _all_digest_entries(digest)
    assert len(entries) > 0
    for entry in entries:
        text = entry["suggestion"]["text"]
        assert 0 < len(text) <= 300
        assert "\n" not in text
        assert entry["item_key"] in text
        assert text.split()[0].lower() in {"review", "ask", "decide", "close", "check", "confirm", "unblock", "merge"}

    # 2) Overlong multi-line output is clamped to <= 300 chars and single line
    overlong_reply = "Ask the owner to decide on this item today. " + ("Extra commentary that repeats forever. " * 20) + "\nSecond paragraph"
    clamped = scrum_suggest.clean(overlong_reply)
    assert clamped is not None
    assert 0 < len(clamped) <= 300
    assert "\n" not in clamped
