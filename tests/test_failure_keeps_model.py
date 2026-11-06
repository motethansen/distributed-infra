"""Failed agent_run/code_review/write_article/write_post results keep the model that was paid for."""
import asyncio

import pytest

from shared.models import Task, TaskType

FAIL = {"ok": False, "error": "boom", "agent": "deepseek", "model": "deepseek-chat",
        "usage": {"tokens_in": 3, "cost_usd": None}, "failover": []}


def _task(ttype, **pl):
    return Task(type=ttype, created_by="t", payload=pl)


def test_agent_run(monkeypatch):
    from agents import runner
    from worker.handlers import agent_run

    async def fake(**k):
        return dict(FAIL)
    monkeypatch.setattr(agent_run, "run_agent", fake, raising=False)
    monkeypatch.setattr(runner, "run_agent", fake)
    r = asyncio.run(agent_run.handle_agent_run(_task(TaskType.agent_run, prompt="p", agent="deepseek")))
    assert r["needs_human"] and r["model"] == "deepseek-chat" and r["agent"] == "deepseek"


def test_code_review(monkeypatch, tmp_path):
    from agents import runner
    from worker.handlers import code_review

    async def fake(**k):
        return dict(FAIL)
    monkeypatch.setattr(runner, "run_agent", fake)
    r = asyncio.run(code_review.handle_code_review(_task(TaskType.code_review, target=str(tmp_path))))
    assert r["needs_human"] and r["model"] == "deepseek-chat"


@pytest.mark.parametrize("mod,handler,agent_mod,ttype", [
    ("write_article", "handle_write_article", "content_agent", TaskType.write_article),
    ("write_post", "handle_write_post", "social_agent", TaskType.write_post)])
def test_content_handlers(monkeypatch, mod, handler, agent_mod, ttype):
    import importlib
    am = importlib.import_module(f"agents.{agent_mod}")
    m = importlib.import_module(f"worker.handlers.{mod}")

    async def fake(**k):
        return dict(FAIL)
    monkeypatch.setattr(am, "run", fake)
    r = asyncio.run(getattr(m, handler)(_task(ttype, prompt="p")))
    assert r["needs_human"] and r["model"] == "deepseek-chat"
