#!/usr/bin/env python3
"""
Gemini agent — uses google-genai SDK with GEMINI_API_KEY.

CLI usage:
  python agents/gemini_agent.py "explain async/await in Python"
  python agents/gemini_agent.py "review this code" --model gemini-1.5-flash
"""
from __future__ import annotations

import asyncio
import os
import sys


async def run(prompt: str, model: str = "gemini-2.5-flash") -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return {"error": "GEMINI_API_KEY not set", "agent": "gemini", "ok": False}
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        return {"agent": "gemini", "model": model, "response": response.text, "ok": True}
    except Exception as exc:
        return {"error": str(exc), "agent": "gemini", "ok": False}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="gemini-2.0-flash")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
