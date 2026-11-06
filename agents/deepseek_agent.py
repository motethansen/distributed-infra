#!/usr/bin/env python3
"""
DeepSeek agent — uses the DeepSeek REST API directly (OpenAI-compatible, no CLI).
Cheap reasoning/coding provider.

Requires DEEPSEEK_API in the worker's environment (.env file or launchd plist).

Privacy: DeepSeek is China-hosted — never route email/finance/calendar/personal
data here. Non-sensitive coding/reasoning/bulk only (enforced by the #5 router).

CLI usage:
  python agents/deepseek_agent.py "explain list comprehensions in Python"
  python agents/deepseek_agent.py --model deepseek-reasoner "prove ..."
"""
from __future__ import annotations

import asyncio
import functools
import os
import re
import sys
from pathlib import Path

# deepseek-chat = V3 (general/coding); deepseek-reasoner = R1 (chain-of-thought).
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEFAULT_TIMEOUT_SECS = int(os.environ.get("DEEPSEEK_AGENT_TIMEOUT_SECS", "120"))
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

# DeepSeek is an API model: it has no shell and no file access. Given an agentic prompt ("check out the
# branch, run the tests") it will sometimes imitate a tool call and write it out as text. Nothing runs, and
# the reply looks like a success. Tell it up front, and catch the imitation when it happens anyway.
SYSTEM_PROMPT = (
    "You are a text-only assistant. You have no shell, no file access and no tools: you cannot run commands, "
    "check out branches, read or edit files, or browse. Never write tool calls, function calls or command "
    "invocations as markup or as pseudo-markup. When asked to write code, reply with the complete content of "
    "each file in a fenced code block, preceded by a line `FILE: relative/path`. If the request needs something "
    "only a tool could do, say so in one sentence and still return the code you can write."
)
RETRY_REMINDER = (
    "\n\nReminder: you cannot run commands or call tools. Do not output tool-call markup. "
    "Answer with the code or text itself."
)

# Real tool-call wrapper syntax only (not the word "DSML" or "invoke" in prose): DeepSeek's DSML tags (full-width
# or ASCII bars), <function_calls>/<tool_call(s)>/<tool_use> tags, <invoke name=...>, and the old special tokens.
_WRAPPER_RE = re.compile(
    r"<\s*/?\s*[｜|]+\s*DSML\s*[｜|]+"
    r"|<\s*/?\s*(?:function_calls|tool_calls?|tool_use)\b[^<>]*>"
    r"|<\s*invoke\s+name\s*="
    r"|<[｜|]\s*tool[▁_ ]calls?[▁_ ](?:begin|sep)",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# Structured marker the runner/failover read; the wording below is only the human message.
FAILURE_KIND_TEXT_ONLY = "text_only"
TEXT_ONLY_ERROR = ("deepseek printed a tool call as text: it is text-only and cannot run commands or edit files, "
                   "so no work was done")


# A fence directly preceded by the system prompt's `FILE: path` line is file content, whatever it contains.
_FILE_LABEL_RE = re.compile(r"(?:^|\n)[ \t]*FILE:[ \t]*\S[^\n]*\s*$")


def _split(text: str) -> tuple[bool, bool]:
    """(imitation, real_code): does the reply contain a printed tool call, and does real code remain once
    imitation fences are discarded? A fence labelled `FILE: path` is real code. Any other fence with a wrapper
    anywhere in its body is imitation; one without is real code. A wrapper outside any fence is imitation."""
    text = text or ""
    imitation = real_code = False
    for m in _FENCE_RE.finditer(text):
        body = m.group(1)
        if not body.strip():
            continue
        if _FILE_LABEL_RE.search(text[:m.start()]) or not _WRAPPER_RE.search(body):
            real_code = True
        else:
            imitation = True        # an unlabeled fence holding a fake call is not code
    outside = _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", text))
    if _WRAPPER_RE.search(outside):
        imitation = True            # a wrapper outside any fence always counts
    return imitation, real_code


def looks_like_tool_call(text: str) -> bool:
    """True when the reply contains tool-call markup that DeepSeek wrote as text (so nothing was run)."""
    return _split(text)[0]


def _is_text_only_failure(text: str) -> bool:
    imitation, real_code = _split(text)
    return imitation and not real_code


def _failure(e: Exception) -> dict:
    import httpx
    if isinstance(e, httpx.HTTPStatusError):
        return {"error": f"DeepSeek API error {e.response.status_code}: {e.response.text[:300]}", "agent": "deepseek", "ok": False}
    return {"error": str(e), "agent": "deepseek", "ok": False}


PRICES_PATH = Path(os.environ.get("LLM_PRICES_FILE") or Path(__file__).resolve().parent.parent / "config" / "llm_prices.yaml")
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")


@functools.lru_cache(maxsize=1)
def _load_prices() -> dict:
    """The deepseek section of config/llm_prices.yaml ({model: {input_fresh, input_cache_hit, output}}).
    Cached; an unreadable or malformed file gives {} so cost_usd falls back to None."""
    try:
        import yaml
        section = (yaml.safe_load(PRICES_PATH.read_text()) or {}).get("deepseek")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sum_usage(*usages) -> dict | None:
    """Add up raw API usage dicts. A key no call reported stays absent; no usage at all gives None."""
    total: dict = {}
    for u in usages:
        if not isinstance(u, dict):
            continue
        for k in _USAGE_KEYS:
            if _num(u.get(k)) is not None:
                total[k] = total.get(k, 0) + u[k]
    return total or None


def _cost_usd(usage: dict, model: str) -> float | None:
    """USD cost of a (summed) usage dict from the price table; None when the model or its prices are unknown."""
    p = _load_prices().get(model)
    if not isinstance(p, dict) or any(_num(p.get(k)) is None for k in ("input_fresh", "input_cache_hit", "output")):
        return None
    tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if tin is None and tout is None:
        return None
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None and miss is None:
        hit, miss = 0, tin or 0
    else:
        hit, miss = hit or 0, miss if miss is not None else max((tin or 0) - (hit or 0), 0)
    return (miss * p["input_fresh"] + hit * p["input_cache_hit"] + (tout or 0) * p["output"]) / 1_000_000


def _with_usage(result: dict, usage, model: str) -> dict:
    """Stamp agent, model and usage (tokens_in, tokens_out, cost_usd) on a result. `usage` is the raw API usage
    dict, already summed across the first call and any retry. Fields the API did not report are None, not 0."""
    result["agent"] = "deepseek"
    result["model"] = model
    u = usage if isinstance(usage, dict) else {}
    if any(_num(u.get(k)) is not None for k in _USAGE_KEYS):
        result["usage"] = {"tokens_in": _num(u.get("prompt_tokens")), "tokens_out": _num(u.get("completion_tokens")),
                           "cost_usd": _cost_usd(u, model)}
    return result


async def run(prompt: str, model: str = "", cwd: str | None = None, timeout: int | None = None) -> dict:
    api_key = os.environ.get("DEEPSEEK_API", "")
    if not api_key:
        return {
            "error": "DEEPSEEK_API not set. Add it to the worker .env file.",
            "agent": "deepseek",
            "ok": False,
        }

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed. Run: pip install httpx", "agent": "deepseek", "ok": False}

    effective_model = model or DEFAULT_MODEL
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECS

    async def _ask(client, user_prompt: str) -> dict:
        resp = await client.post(
            DEEPSEEK_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": effective_model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user_prompt}],
                "temperature": 0.7,
            },
        )
        resp.raise_for_status()
        return resp.json()

    try:
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            data = await _ask(client, prompt)
            first = data["choices"][0]["message"]["content"]
            usage = _sum_usage(data.get("usage"))
            if looks_like_tool_call(first):
                # One retry with the reminder; the usage of both calls is summed.
                try:
                    retry = await _ask(client, prompt + RETRY_REMINDER)
                except Exception as e:
                    return _with_usage(_failure(e), usage, effective_model)   # the first call was paid for
                usage = _sum_usage(usage, retry.get("usage"))
                data = retry
    except Exception as e:
        return _failure(e)

    text = data["choices"][0]["message"]["content"]
    if _is_text_only_failure(text):
        return _with_usage({"error": TEXT_ONLY_ERROR, "agent": "deepseek", "ok": False,
                            "failure_kind": FAILURE_KIND_TEXT_ONLY}, usage, effective_model)
    return _with_usage({"response": text, "ok": True}, usage, effective_model)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
