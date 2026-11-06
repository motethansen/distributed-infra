"""DeepSeek is text-only. When it imitates a tool call instead of answering, the run must fail loudly and the
task must move to an agent that can do the work, not be reported as a success. Nothing here calls a provider."""
from __future__ import annotations

import asyncio

import pytest

from agents import deepseek_agent as ds
from agents import failover as fo
from agents import runner

DSML = ("I'll start by checking the repo.\n\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name=\"Bash\">\n"
        "<｜｜DSML｜｜ parameter name=\"command\">git status</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(fo, "COOLDOWN_FILE", tmp_path / "cooldowns.json")
    monkeypatch.setenv("DEEPSEEK_API", "test-key-not-real")


# ── detection ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    DSML,
    "<||DSML||function_calls><||DSML||invoke name=\"x\">",
    "<function_calls>\n<invoke name=\"Bash\">\n</invoke>\n</function_calls>",
    "<tool_call>{\"name\": \"bash\"}</tool_call>",
    "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>bash",
])
def test_tool_call_markup_is_detected(text):
    assert ds.looks_like_tool_call(text)


@pytest.mark.parametrize("text", [
    "FILE: app.py\n```python\nprint('hello')\n```",
    "DSML is DeepSeek's markup for tool calls; the invoke tag is described in their docs.",
    "Call the <invoke> helper, or write `<tool_call>` in your prompt.",
    "A tool call is when a model asks the host to run a function.",
    "Use the invoke() helper, then call functions in order.",
    "",
    None,
])
def test_normal_text_is_not_flagged(text):
    assert not ds.looks_like_tool_call(text)


# ── the agent ──────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, content, usage=(10, 5)):
        self._c, self._u = content, usage

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._c}}],
                "usage": {"prompt_tokens": self._u[0], "completion_tokens": self._u[1]}}


def _patch_http(monkeypatch, replies):
    sent = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(json)
            return _Resp(replies[min(len(sent) - 1, len(replies) - 1)])

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return sent


def test_every_request_carries_the_no_tools_system_prompt(monkeypatch):
    sent = _patch_http(monkeypatch, ["FILE: a.py\n```python\nx = 1\n```"])
    out = asyncio.run(ds.run("write a.py"))
    assert out["ok"] and "x = 1" in out["response"]
    assert sent[0]["messages"][0]["role"] == "system"
    assert "no tools" in sent[0]["messages"][0]["content"]
    assert sent[0]["messages"][1] == {"role": "user", "content": "write a.py"}


def test_a_printed_tool_call_is_retried_once_then_succeeds(monkeypatch):
    sent = _patch_http(monkeypatch, [DSML, "FILE: a.py\n```python\nx = 1\n```"])
    out = asyncio.run(ds.run("write a.py"))
    assert out["ok"] and len(sent) == 2
    assert "cannot run commands" in sent[1]["messages"][1]["content"]
    assert out["usage"] == {"tokens_in": 20, "tokens_out": 10}          # both calls are counted


def test_a_printed_tool_call_twice_is_a_failure_not_a_success(monkeypatch):
    _patch_http(monkeypatch, [DSML, DSML])
    out = asyncio.run(ds.run("check out the branch and run the tests"))
    assert out["ok"] is False
    assert out["error"] == ds.TEXT_ONLY_ERROR
    assert "response" not in out
    assert out["failure_kind"] == "text_only"
    assert fo.classify(out["error"], "deepseek", out["failure_kind"]) == "text_only"
    assert fo.classify("anything", "deepseek", "text_only") == "text_only"      # structured, not the wording
    assert fo.classify(out["error"], "claude") is None                          # only DeepSeek can be text-only


def test_markup_next_to_real_code_is_kept(monkeypatch):
    _patch_http(monkeypatch, [DSML + "\n\nFILE: a.py\n```python\nx = 1\n```"])
    out = asyncio.run(ds.run("write a.py"))
    assert out["ok"] and "x = 1" in out["response"]


# ── failover: the work moves to an agent that can do it ───────────────────────
def _fake_agents(monkeypatch, behaviour):
    calls = []
    models = _MODELS
    models.clear()

    def make(name):
        async def run(**kw):
            calls.append(name)
            models.append(kw.get("model", ""))
            return dict(behaviour[name])
        return run

    monkeypatch.setattr(runner, "_load_agents", lambda: None)
    monkeypatch.setattr(runner, "AGENTS", {n: make(n) for n in ("claude", "codex", "agy", "deepseek", "content", "social")})
    return calls


_MODELS: list = []
FAIL = {"ok": False, "agent": "deepseek", "error": ds.TEXT_ONLY_ERROR}
OK = lambda n: {"ok": True, "agent": n, "response": f"done by {n}"}  # noqa: E731


def test_text_only_failure_goes_to_claude_first_not_a_read_only_agent(monkeypatch):
    calls = _fake_agents(monkeypatch, {"deepseek": FAIL, "claude": OK("claude"), "codex": OK("codex"), "agy": OK("agy")})
    out = asyncio.run(runner.run_agent(agent="deepseek", prompt="edit the repo", cwd="/tmp"))
    assert out["ok"] and out["agent"] == "claude"
    assert calls == ["deepseek", "claude"]                              # codex (read-only) was never tried
    assert _MODELS[1] == "sonnet"
    assert out["failover"][0]["agent"] == "deepseek" and out["failover"][0]["reason"] == "text_only"
    assert out["answered_by"] == "claude"


def test_claude_sonnet_is_put_first_even_when_the_class_chain_starts_elsewhere(monkeypatch):
    # the research class lists agy right after deepseek; a text-only failure still goes to Claude Sonnet first
    calls = _fake_agents(monkeypatch, {"deepseek": FAIL, "claude": OK("claude"), "codex": OK("codex"), "agy": OK("agy")})
    out = asyncio.run(runner.run_agent(agent="deepseek", prompt="edit the repo", task_kind="research"))
    assert out["ok"] and out["agent"] == "claude"
    assert calls == ["deepseek", "claude"]
    assert _MODELS[1] == "sonnet"


def test_text_only_failure_sets_no_cooldown(monkeypatch):
    _fake_agents(monkeypatch, {"deepseek": FAIL, "claude": OK("claude"), "codex": OK("codex"), "agy": OK("agy")})
    asyncio.run(runner.run_agent(agent="deepseek", prompt="x"))
    assert fo.cooling_until("deepseek") is None                         # it can still answer plain questions


def test_if_claude_also_fails_the_task_stops_with_a_clear_error(monkeypatch):
    _fake_agents(monkeypatch, {"deepseek": FAIL, "claude": {"ok": False, "agent": "claude", "error": "AssertionError: 3 tests failed"},
                               "codex": OK("codex"), "agy": OK("agy")})
    out = asyncio.run(runner.run_agent(agent="deepseek", prompt="x"))
    assert out["ok"] is False                                           # a real failure is not retried elsewhere
    assert "3 tests failed" in out["error"]


# ── review follow-ups ──────────────────────────────────────────────────────────
FAKE_FENCE = '```xml\n<tool_call>{"name":"bash"}</tool_call>\n```'


def test_a_fence_holding_only_the_fake_call_is_a_failure(monkeypatch):
    _patch_http(monkeypatch, [FAKE_FENCE, FAKE_FENCE])
    out = asyncio.run(ds.run("run the tests"))
    assert out["ok"] is False and out["failure_kind"] == "text_only"


def test_a_fake_call_fence_plus_an_empty_fence_still_fails(monkeypatch):
    # the old check exempted the reply as soon as any fence existed; an empty one is not code either
    reply = "Sure:\n" + FAKE_FENCE + "\n```\n   \n```"
    _patch_http(monkeypatch, [reply, reply])
    assert asyncio.run(ds.run("x"))["ok"] is False


def test_fake_call_fence_is_detected_and_real_fence_beside_it_is_kept():
    assert ds.looks_like_tool_call(FAKE_FENCE)
    assert ds._is_text_only_failure(FAKE_FENCE)
    both = FAKE_FENCE + "\nFILE: a.py\n```python\nx = 1\n```"
    assert ds.looks_like_tool_call(both) and not ds._is_text_only_failure(both)


def test_commentary_before_a_fake_call_in_a_fence_is_still_imitation(monkeypatch):
    reply = "```\nI'll run this now:\n<tool_call>{\"name\": \"bash\"}</tool_call>\n```"
    sent = _patch_http(monkeypatch, [reply, reply])
    out = asyncio.run(ds.run("run the tests"))
    assert len(sent) == 2 and out["ok"] is False and out["failure_kind"] == "text_only"


@pytest.mark.parametrize("label", ["FILE: fixture.xml\n", "FILE: fixture.xml\n\n  \n"])
def test_a_file_labeled_xml_fixture_is_content_not_imitation(monkeypatch, label):
    reply = label + "```xml\n<tool_call>{\"name\": \"bash\"}</tool_call>\n```"
    sent = _patch_http(monkeypatch, [reply])
    out = asyncio.run(ds.run("write an XML fixture"))
    assert out["ok"] is True and len(sent) == 1 and "<tool_call>" in out["response"]
    assert not ds.looks_like_tool_call(reply)


def test_label_is_case_sensitive_and_must_be_adjacent():
    fence = "```xml\n<tool_call>x</tool_call>\n```"
    assert ds._is_text_only_failure("file: f.xml\n" + fence)
    assert ds._is_text_only_failure("FILE: f.xml\nsome words\n" + fence)


def test_unlabeled_xml_example_beside_real_code_is_kept_but_flagged(monkeypatch):
    reply = ("Example:\n```xml\n<tool_call>x</tool_call>\n```\nFILE: a.py\n```python\nx = 1\n```")
    assert ds.looks_like_tool_call(reply) and not ds._is_text_only_failure(reply)
    _patch_http(monkeypatch, [reply])
    out = asyncio.run(ds.run("x"))
    assert out["ok"] is True and "x = 1" in out["response"]


def test_dsml_in_prose_does_not_trigger_a_retry(monkeypatch):
    sent = _patch_http(monkeypatch, ["DSML is a markup DeepSeek uses; the <invoke> tag is one part.\nFILE: a.py\n```python\nx = 1\n```"])
    out = asyncio.run(ds.run("explain DSML"))
    assert out["ok"] and len(sent) == 1


def test_retry_error_still_carries_the_first_calls_usage(monkeypatch):
    import httpx
    sent = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(json)
            if len(sent) == 1:
                return _Resp(DSML, usage=(30, 7))
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    out = asyncio.run(ds.run("x"))
    assert out["ok"] is False and len(sent) == 2
    assert out["usage"] == {"tokens_in": 30, "tokens_out": 7}
    assert "failure_kind" not in out                                    # a timeout is not a text-only failure


def test_text_only_failure_keeps_its_usage_across_both_calls(monkeypatch):
    _patch_http(monkeypatch, [DSML, DSML])
    out = asyncio.run(ds.run("x"))
    assert out["usage"] == {"tokens_in": 20, "tokens_out": 10}


def test_claude_already_attempted_is_not_retried(monkeypatch):
    calls = _fake_agents(monkeypatch, {"deepseek": FAIL, "claude": {"ok": False, "agent": "claude", "error": "usage limit reached"},
                                       "codex": OK("codex"), "agy": OK("agy")})
    out = asyncio.run(runner.run_agent(agent="deepseek", prompt="x"))
    assert calls.count("claude") == 1                                   # promoted once, never re-queued
    assert out["ok"]


def test_queue_exhaustion_calls_each_agent_once(monkeypatch):
    bad = lambda n: {"ok": False, "agent": n, "error": "usage limit reached"}  # noqa: E731
    calls = _fake_agents(monkeypatch, {"deepseek": FAIL, "claude": bad("claude"), "codex": bad("codex"), "agy": bad("agy")})
    out = asyncio.run(runner.run_agent(agent="deepseek", prompt="x"))
    assert out["ok"] is False
    assert len(calls) == len(set(calls))                                # no agent called twice


def test_claude_is_not_injected_when_the_caller_excluded_it(monkeypatch):
    calls = _fake_agents(monkeypatch, {"deepseek": FAIL, "claude": OK("claude"), "codex": OK("codex"), "agy": OK("agy")})
    asyncio.run(runner.run_agent(agent="deepseek", prompt="x", exclude={"claude"}))
    assert "claude" not in calls


def test_classify_uses_the_structured_kind_only_for_deepseek():
    assert fo.classify("anything at all", "deepseek", "text_only") == "text_only"     # wording does not matter
    assert fo.classify("anything at all", "claude", "text_only") is None              # other agents cannot claim it
    assert fo.classify(ds.TEXT_ONLY_ERROR, "codex") is None                           # nor can their error text
    assert fo.classify(ds.TEXT_ONLY_ERROR, "deepseek") == "text_only"                 # deepseek-only text fallback


def test_a_non_deepseek_agent_cannot_trigger_the_text_only_handoff(monkeypatch):
    spoof = {"ok": False, "agent": "codex", "error": ds.TEXT_ONLY_ERROR, "failure_kind": "text_only"}
    calls = _fake_agents(monkeypatch, {"codex": spoof, "claude": OK("claude"), "deepseek": OK("deepseek"), "agy": OK("agy")})
    out = asyncio.run(runner.run_agent(agent="codex", prompt="x"))
    assert out["ok"] is False and calls == ["codex"]
