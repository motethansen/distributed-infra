"""Fleet MCP server (clients/fleet-mcp/server.py) against a fake orchestrator."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients" / "fleet-mcp"))
import server  # noqa: E402


class FakeQueue:
    """Minimal orchestrator: POST /tasks, GET /tasks/{id}, GET /tasks, GET /machines."""

    def __init__(self, finish_with=None, down_urls=()):
        self.tasks: dict[str, dict] = {}
        self.created: list[dict] = []
        self.finish_with = finish_with or (lambda payload: {"ok": True, "agent": "claude", "response": "hi"})
        self.down_urls = set(down_urls)
        self.board = {"projects": [{"project": "demo", "closed": 3, "stories": 9, "counts": {"in_review": 1},
                                    "active_items": [{"id": "D-1", "status": "in_review", "title": "T", "evidence": "PR 2", "x": 1}]}],
                      "digest": {"as_of": "2026-10-31", "review_queue": [{"project": "demo", "id": "D-1", "title": "T", "evidence": "PR 2"}],
                                 "decisions": [{"project": "other", "kind": "stalled", "text": "other stalled"}],
                                 "adjustments": ["1 stories are waiting"], "projects": [{"project": "demo"}], "opus": None}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        base = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
        if base in self.down_urls:
            raise httpx.ConnectError("down")
        if request.headers.get("x-secret-key") != "s3cret":
            return httpx.Response(401, json={"detail": "bad key"})
        path = request.url.path
        if request.method == "POST" and path == "/tasks":
            body = json.loads(request.content)
            tid = f"t{len(self.tasks) + 1}"
            res = self.finish_with(body["payload"])
            status = "done" if res.get("ok") else "needs_human"
            self.tasks[tid] = {"id": tid, "type": body["type"], "status": status, "result": res,
                               "notes": None if res.get("ok") else res.get("error"), "payload": body["payload"]}
            self.created.append(body)
            return httpx.Response(201, json={"id": tid, "status": "pending"})
        if request.method == "GET" and path.startswith("/tasks/"):
            return httpx.Response(200, json=self.tasks[path.rsplit("/", 1)[1]])
        if request.method == "GET" and path == "/tasks":
            return httpx.Response(200, json=list(self.tasks.values()))
        if path == "/scrum/sprints":
            return httpx.Response(200, json=self.board)
        if path == "/machines":
            return httpx.Response(200, json=[{"name": "mac-mini", "worker_active": True, "last_seen_ago_secs": 3,
                                              "capabilities": ["agent_run"]}])
        return httpx.Response(404)


def _tools(tmp_path, queue: FakeQueue, urls=("http://a:8000",)):
    cfg = {"secret": "s3cret", "urls": list(urls), "state_file": tmp_path / "state.json"}
    fleet = server.Fleet(cfg, transport=httpx.MockTransport(queue.handler))
    return server.Tools(fleet, server.State(cfg["state_file"]))


def test_ask_returns_answer_and_who_answered(tmp_path):
    q = FakeQueue(lambda p: {"ok": True, "agent": "codex", "response": "42",
                             "failover": [{"agent": "claude", "reason": "limit", "until": "2026-10-28T12:50:00+00:00"}]})
    r = asyncio.run(_tools(tmp_path, q).ask("meaning of life?"))
    assert r["status"] == "done" and r["response"] == "42" and r["answered_by"] == "codex"
    assert r["failover"] == [{"agent": "claude", "reason": "limit", "until": "2026-10-28T12:50:00+00:00"}]
    sent = q.created[0]
    assert sent["type"] == "agent_run" and sent["payload"]["task_kind"] == "default"
    assert sent["payload"]["failover"] is True and "_target_machine" not in sent["payload"]


def test_conversation_sends_history_and_records_replies(tmp_path):
    q = FakeQueue(lambda p: {"ok": True, "agent": "codex" if p.get("resume") else "claude",
                             "response": f"reply to {p['prompt']}", "session_id": p.get("session_id")})
    t = _tools(tmp_path, q)
    asyncio.run(t.ask("plan Kyoto", agent="claude", conversation="trip"))
    asyncio.run(t.ask("and day 2?", conversation="trip"))
    first, second = q.created[0]["payload"], q.created[1]["payload"]
    assert first["resume"] is False and "history" not in first
    assert second["resume"] is True and second["agent"] == "claude"
    assert second["session_id"] == first["session_id"]
    assert [h["role"] for h in second["history"]] == ["user", "assistant"]
    assert second["history"][1] == {"role": "assistant", "text": "reply to plan Kyoto", "agent": "claude"}
    conv = server.State(tmp_path / "state.json").load()["conversations"]["trip"]
    assert conv["history"][-1]["agent"] == "codex"  # the fallback's answer is remembered
    assert server.State(tmp_path / "state.json").load()["tasks"] == {}


def test_conversation_survives_a_second_server_process(tmp_path):
    q = FakeQueue()
    asyncio.run(_tools(tmp_path, q).ask("hello", conversation="c"))
    asyncio.run(_tools(tmp_path, q).ask("again", conversation="c"))  # e.g. Codex after Claude Code
    assert q.created[1]["payload"]["resume"] is True


def test_end_conversation_starts_fresh(tmp_path):
    q = FakeQueue()
    t = _tools(tmp_path, q)
    asyncio.run(t.ask("one", conversation="c"))
    assert t.end_conversation("c")["ended"] is True
    asyncio.run(t.ask("two", conversation="c"))
    assert q.created[1]["payload"]["resume"] is False


def test_failed_task_reports_error(tmp_path):
    q = FakeQueue(lambda p: {"ok": False, "error": "every agent in the chain is unavailable"})
    r = asyncio.run(_tools(tmp_path, q).ask("x"))
    assert r["status"] == "needs_human" and "unavailable" in r["error"]


def test_falls_back_to_second_orchestrator(tmp_path):
    q = FakeQueue(down_urls={"http://a:8000"})
    r = asyncio.run(_tools(tmp_path, q, urls=("http://a:8000", "http://b:8000")).ask("x"))
    assert r["status"] == "done"


def test_no_orchestrator_is_an_error_not_a_crash(tmp_path):
    q = FakeQueue(down_urls={"http://a:8000"})
    r = asyncio.run(_tools(tmp_path, q).machines())
    assert "no orchestrator reachable" in r["error"]


def test_machines_and_tasks(tmp_path):
    q = FakeQueue()
    t = _tools(tmp_path, q)
    assert asyncio.run(t.machines())["machines"][0] == {"name": "mac-mini", "worker_active": True,
                                             "last_seen_secs": 3, "can_run_agents": True}
    asyncio.run(t.ask("x"))
    assert asyncio.run(t.tasks())["tasks"][0]["status"] == "done"


def test_secret_comes_from_dotenv_not_harness_config(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('SECRET_KEY="from-file"\nORCHESTRATOR_URLS=http://x:8000, http://y:8000\n')
    cfg = server.load_config(env={}, dotenv_path=env_file)
    assert cfg["secret"] == "from-file" and cfg["urls"] == ["http://x:8000", "http://y:8000"]
    with pytest.raises(server.FleetError):
        server.Fleet(dict(cfg, secret=""))


def test_wait_is_capped():
    assert server.MAX_WAIT <= 110


def test_role_and_scrum_tags_reach_the_queue_without_a_forced_task_kind(tmp_path):
    q = FakeQueue()
    r = asyncio.run(_tools(tmp_path, q).ask("review it", role="reviewer", project="budgetapp", sprint="S5",
                                            story="BA-1", conversation="rev"))
    assert r["status"] == "done"
    p = q.created[0]["payload"]
    assert (p["role"], p["project"], p["sprint"], p["story"]) == ("reviewer", "budgetapp", "S5", "BA-1")
    assert "task_kind" not in p and "agent" not in p      # the orchestrator resolves the role, nothing pre-empts it


def test_without_a_role_the_default_route_is_unchanged(tmp_path):
    q = FakeQueue()
    asyncio.run(_tools(tmp_path, q).ask("hello"))
    p = q.created[0]["payload"]
    assert p["task_kind"] == "default" and "role" not in p


def test_scrum_digest_and_one_project_view_and_an_old_orchestrator(tmp_path):
    q = FakeQueue()
    t = _tools(tmp_path, q)
    d = asyncio.run(t.scrum())
    assert d["review_queue"][0]["id"] == "D-1" and d["adjustments"] == ["1 stories are waiting"] and d["decisions"][0]["kind"] == "stalled"
    one = asyncio.run(t.scrum("demo"))
    assert one["in_flight"] == [{"id": "D-1", "status": "in_review", "title": "T", "evidence": "PR 2"}]
    assert one["review_queue"][0]["id"] == "D-1" and one["decisions"] == []                 # other projects' decisions are not shown
    assert "unknown project" in asyncio.run(t.scrum("nosuch"))["error"]
    q.board = {"projects": []}
    assert "too old" in asyncio.run(t.scrum())["error"]
