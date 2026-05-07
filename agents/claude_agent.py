#!/usr/bin/env python3
"""
Claude agent — uses `claude -p` CLI (Claude Code, covered by subscription).

CLI usage:
  python agents/claude_agent.py "write a hello world in Python"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


def _find_cli() -> str | None:
    for p in [
        shutil.which("claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]:
        if p and os.path.isfile(p):
            return p
    return None


async def run(prompt: str, model: str = "") -> dict:
    cli = _find_cli()
    if not cli:
        return {"error": "claude CLI not found — run: npm install -g @anthropic-ai/claude-code", "agent": "claude", "ok": False}

    args = [cli, "-p", prompt]
    if model:
        args += ["--model", model]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "claude CLI timed out", "agent": "claude", "ok": False}

    if proc.returncode != 0:
        return {"error": stderr.decode().strip(), "agent": "claude", "ok": False}

    return {"agent": "claude", "model": model or "claude-default", "response": stdout.decode().strip(), "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
