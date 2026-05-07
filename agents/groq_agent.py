#!/usr/bin/env python3
"""
Groq agent — fast inference via GROQ_API_KEY (LLaMA 3.3 70B default).

CLI usage:
  python agents/groq_agent.py "write a Python function to sort a list"
  python agents/groq_agent.py "debug this" --model llama-3.3-70b-versatile
"""
from __future__ import annotations

import asyncio
import os
import sys


async def run(prompt: str, model: str = "llama-3.3-70b-versatile") -> dict:
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        return {"error": "GROQ_API_KEY not set", "agent": "groq", "ok": False}
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        text = completion.choices[0].message.content
        return {"agent": "groq", "model": model, "response": text, "ok": True}
    except Exception as exc:
        return {"error": str(exc), "agent": "groq", "ok": False}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="llama-3.3-70b-versatile")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
