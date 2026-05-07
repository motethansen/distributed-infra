#!/usr/bin/env python3
"""
Claude agent — uses the `claude` CLI in print (-p) mode.
Falls back to Anthropic SDK if ANTHROPIC_API_KEY is set.

CLI usage:
  python agents/claude_agent.py "write a hello world in Python"
  python agents/claude_agent.py "review this code" --model claude-sonnet-4-6
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


async def run(prompt: str, model: str = "claude-sonnet-4-6") -> dict:
    # Prefer SDK if key is available
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        return await _run_sdk(prompt, model, api_key)
    # Fall back to claude CLI
    cli = shutil.which("claude")
    if cli:
        return await _run_cli(prompt, cli)
    return {"error": "No ANTHROPIC_API_KEY and claude CLI not found on PATH", "ok": False}


async def _run_sdk(prompt: str, model: str, api_key: str) -> dict:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        return {"agent": "claude", "model": model, "response": text, "ok": True}
    except Exception as exc:
        return {"error": str(exc), "agent": "claude", "ok": False}


async def _run_cli(prompt: str, cli_path: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        cli_path, "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "claude CLI timed out", "agent": "claude", "ok": False}

    if proc.returncode != 0:
        return {"error": stderr.decode().strip(), "agent": "claude", "ok": False}

    return {"agent": "claude", "model": "claude-cli", "response": stdout.decode().strip(), "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
