#!/usr/bin/env python3
"""
Groq agent — uses the Groq REST API directly (no CLI required).
Fast inference via Groq's LPU hardware.

Requires GROQ_API_KEY in the worker's environment (.env file or launchd plist).

CLI usage:
  python agents/groq_agent.py "explain list comprehensions in Python"
"""
from __future__ import annotations

import asyncio
import os
import sys

DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
DEFAULT_TIMEOUT_SECS = int(os.environ.get("GROQ_AGENT_TIMEOUT_SECS", "120"))
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


async def run(prompt: str, model: str = "", cwd: str | None = None, timeout: int | None = None) -> dict:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return {
            "error": "GROQ_API_KEY not set. Add it to the worker .env file.",
            "agent": "groq",
            "ok": False,
        }

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed. Run: pip install httpx", "agent": "groq", "ok": False}

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
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"Groq API error {e.response.status_code}: {e.response.text[:300]}", "agent": "groq", "ok": False}
    except Exception as e:
        return {"error": str(e), "agent": "groq", "ok": False}

    text = data["choices"][0]["message"]["content"]
    return {"agent": "groq", "model": effective_model, "response": text, "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
