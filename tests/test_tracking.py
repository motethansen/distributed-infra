"""Executor + token tracking: what each agent reports, what lands on the task row, the summaries."""
import json
import os
import sqlite3
import stat

import pytest

from shared import tracking
from shared.models import Task, TaskStatus, TaskType


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_from_result_reads_agent_model_and_usage():
    r = {"agent": "claude", "model": "sonnet", "ok": True,
         "usage": {"tokens_in": 100, "tokens_out": 20, "cost_usd": 0.0123456789}}
    assert tracking.from_result(r) == {"agent": "claude", "model": "sonnet", "tokens_in": 100,
                                       "tokens_out": 20, "tokens_total": 120, "cost_usd": 0.012346}


def test_from_result_prefers_the_agent_that_answered_over_a_wrapper_name():
    r = {"agent": "content", "answered_by": "agy", "model": "agy-default"}
    assert tracking.from_result(r)["agent"] == "agy"


def test_from_result_keeps_a_codex_style_total_only_and_ignores_garbage():
    assert tracking.from_result({"agent": "codex", "usage": {"tokens_total": 4071}})["tokens_total"] == 4071
    assert "tokens_total" not in tracking.from_result({"agent": "x", "usage": {"tokens_total": "lots", "tokens_in": -5}})
    assert tracking.from_result(None) == {} and tracking.from_result("nope") == {}


def test_from_payload_picks_scrum_tags_only():
    assert tracking.from_payload({"project": "budgetapp", "sprint": "S11", "story": "BA-102", "prompt": "x"}) == \
           {"project": "budgetapp", "sprint": "S11", "story": "BA-102"}
    assert tracking.from_payload({"project": ""}) == {}


def test_summarise_groups_totals_and_counts_distinct_agents_and_machines():
    rows = [
        {"status": "done", "agent": "claude", "assigned_to": "mac-mini", "tokens_in": 10, "tokens_out": 5, "tokens_total": 15,
         "cost_usd": 0.1, "sprint": "S1", "story": "A-1", "started_at": "2026-10-30T01:00:00", "finished_at": "2026-10-30T01:00:10"},
        {"status": "done", "agent": "codex", "assigned_to": "thinkpad-x1", "tokens_total": 100, "sprint": "S1", "story": "A-2"},
        {"status": "failed", "agent": "claude", "assigned_to": "mac-mini", "sprint": "S2"},
        {"status": "pending", "sprint": None},
    ]
    s = {g["key"]: g for g in tracking.summarise(rows, "sprint")}
    assert s["S1"]["tasks"] == 2 and s["S1"]["done"] == 2 and s["S1"]["agent_count"] == 2
    assert s["S1"]["machines"] == ["mac-mini", "thinkpad-x1"] and s["S1"]["stories"] == ["A-1", "A-2"]
    assert s["S1"]["tokens_total"] == 115 and s["S1"]["avg_seconds"] == 10.0
    assert s["S2"]["failed"] == 1 and s["(untagged)"]["open"] == 1
    assert tracking.summarise(rows, "all")[0]["tasks"] == 4


# ── what each agent CLI prints ───────────────────────────────────────────────

def test_claude_json_output_gives_text_usage_and_counts_cache_tokens():
    from agents.claude_agent import parse_json_output
    out = json.dumps({"result": " pong\n", "is_error": False, "total_cost_usd": 0.0005,
                      "usage": {"input_tokens": 2, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 36578,
                                "output_tokens": 4}})
    text, usage, is_err = parse_json_output(out)
    assert text == "pong" and not is_err
    assert usage == {"tokens_in": 36590, "tokens_out": 4, "cost_usd": 0.0005}


def test_claude_plain_or_error_output_falls_back_to_the_raw_text():
    from agents.claude_agent import parse_json_output
    assert parse_json_output("Not logged in · Please run /login") == ("Not logged in · Please run /login", None, False)
    text, _, is_err = parse_json_output(json.dumps({"result": "Credit balance too low", "is_error": True}))
    assert is_err and text == "Credit balance too low"


def test_agy_json_output():
    from agents.agy_agent import parse_json_output
    out = json.dumps({"status": "SUCCESS", "response": "pong\n",
                      "usage": {"input_tokens": 14708, "output_tokens": 13, "thinking_tokens": 8, "total_tokens": 14721}})
    assert parse_json_output(out) == ("pong", {"tokens_in": 14708, "tokens_out": 13, "tokens_total": 14721}, False)
    assert parse_json_output("plain text")[1] is None
    assert parse_json_output(json.dumps({"status": "ERROR", "response": "quota"}))[2] is True


def test_codex_tokens_used_line():
    from agents.codex_agent import parse_tokens_used
    assert parse_tokens_used("codex\npong\ntokens used\n4,071\npong") == 4071
    assert parse_tokens_used("tokens used\n10\n ... tokens used\n2,500") == 2500   # last one wins
    assert parse_tokens_used("nothing here") is None


@pytest.mark.asyncio
async def test_claude_agent_end_to_end_with_a_fake_cli(tmp_path, monkeypatch):
    from agents import claude_agent
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho '{\"result\":\"pong\",\"is_error\":false,\"total_cost_usd\":0.001,"
                    "\"usage\":{\"input_tokens\":3,\"output_tokens\":2}}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(claude_agent, "_find_cli", lambda: str(fake))
    r = await claude_agent.run("hi", "sonnet")
    assert r["ok"] and r["response"] == "pong" and r["usage"] == {"tokens_in": 3, "tokens_out": 2, "cost_usd": 0.001}
    # and the orchestrator would store it:
    assert tracking.from_result(r)["tokens_total"] == 5


# ── the database ─────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("QUEUE_DB_PATH", str(tmp_path / "q.db"))
    import orchestrator.db as m
    importlib.reload(m)
    return m


@pytest.mark.asyncio
async def test_row_records_executor_tokens_and_timings(db):
    t = await db.insert_task(Task(type=TaskType.agent_run, created_by="macbook-pro",
                                  payload={"project": "budgetapp", "sprint": "S11", "story": "BA-102"}))
    assert (await db.get_task(t.id)).sprint == "S11"
    await db.update_task(t.id, {"status": TaskStatus.in_progress, "assigned_to": "mac-mini"})
    first = (await db.get_task(t.id)).started_at
    assert first
    await db.update_task(t.id, {"status": TaskStatus.in_progress})        # a retry must not reset the start
    assert (await db.get_task(t.id)).started_at == first
    await db.update_task(t.id, {"status": TaskStatus.done, "result": {
        "agent": "claude", "model": "sonnet", "response": "ok",
        "usage": {"tokens_in": 40, "tokens_out": 10, "cost_usd": 0.002}}})
    done = await db.get_task(t.id)
    assert (done.agent, done.model, done.tokens_in, done.tokens_out, done.tokens_total, done.cost_usd) == \
           ("claude", "sonnet", 40, 10, 50, 0.002)
    assert done.finished_at and done.finished_at >= first
    assert done.result["response"] == "ok"                                  # the answer itself is untouched


@pytest.mark.asyncio
async def test_needs_human_result_patch_still_records_the_agent(db):
    t = await db.insert_task(Task(type=TaskType.agent_run, created_by="x"))
    await db.update_task(t.id, {"result": {"agent": "codex", "usage": {"tokens_total": 900}}})   # worker PATCHes result first
    await db.update_task(t.id, {"status": TaskStatus.needs_human, "notes": "look"})
    row = await db.get_task(t.id)
    assert row.agent == "codex" and row.tokens_total == 900 and row.finished_at


@pytest.mark.asyncio
async def test_old_database_is_migrated_and_backfilled_without_losing_a_row(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        priority INTEGER NOT NULL DEFAULT 5, payload TEXT NOT NULL DEFAULT '{}', created_by TEXT NOT NULL, assigned_to TEXT,
        result TEXT, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    con.execute("INSERT INTO tasks VALUES ('a','agent_run','done',5,'{\"sprint\":\"S9\"}','m','mac-mini',"
                "'{\"agent\":\"deepseek\",\"model\":\"deepseek-chat\",\"response\":\"hi\"}',NULL,'2026-09-01T00:00:00','2026-09-01T00:01:00')")
    con.execute("INSERT INTO tasks VALUES ('b','run_script','pending',5,'{}','m',NULL,NULL,NULL,'2026-09-02T00:00:00','2026-09-02T00:00:00')")
    con.commit(); con.close()
    monkeypatch.setenv("QUEUE_DB_PATH", str(path))
    import importlib
    import orchestrator.db as m
    importlib.reload(m)
    a, b = await m.get_task("a"), await m.get_task("b")
    assert (a.agent, a.model, a.sprint, a.finished_at) == ("deepseek", "deepseek-chat", "S9", "2026-09-01T00:01:00")
    assert a.result["response"] == "hi" and b.status == TaskStatus.pending and b.agent is None
    assert len(await m.list_tasks(limit=10)) == 2


# ── the endpoint ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scrum_usage_endpoint(db, monkeypatch):
    import importlib
    monkeypatch.setenv("QUEUE_BACKEND", "sqlite"); monkeypatch.setenv("SECRET_KEY", "k")
    import orchestrator.main as main
    importlib.reload(main)
    for agent, tok in (("claude", 100), ("codex", 50), ("claude", 10)):
        t = await main.db.insert_task(Task(type=TaskType.agent_run, created_by="x", payload={"sprint": "S11"}))
        await main.db.update_task(t.id, {"status": TaskStatus.done, "assigned_to": "mac-mini",
                                         "result": {"agent": agent, "usage": {"tokens_total": tok}}})
    out = await main.scrum_usage(group_by="agent", since=None, until=None, project=None, sprint="S11", x_secret_key="k")
    by = {g["key"]: g for g in out["groups"]}
    assert out["tasks"] == 3 and by["claude"]["tokens_total"] == 110 and by["codex"]["tasks"] == 1
    assert out["totals"]["tokens_total"] == 160 and out["totals"]["agent_count"] == 2
    with pytest.raises(Exception):
        await main.scrum_usage(group_by="nonsense", since=None, until=None, project=None, sprint=None, x_secret_key="k")
    with pytest.raises(Exception):
        await main.scrum_usage(group_by="agent", since=None, until=None, project=None, sprint=None, x_secret_key="bad")
