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
import os
import sys

# deepseek-chat = V3 (general/coding); deepseek-reasoner = R1 (chain-of-thought).
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEFAULT_TIMEOUT_SECS = int(os.environ.get("DEEPSEEK_AGENT_TIMEOUT_SECS", "120"))
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"


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

    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }

    try:
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            resp = await client.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"DeepSeek API error {e.response.status_code}: {e.response.text[:300]}", "agent": "deepseek", "ok": False}
    except Exception as e:
        return {"error": str(e), "agent": "deepseek", "ok": False}

    text = data["choices"][0]["message"]["content"]
    return {"agent": "deepseek", "model": effective_model, "response": text, "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
