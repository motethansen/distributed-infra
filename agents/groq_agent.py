#!/usr/bin/env python3
"""
Cursor Agent — uses `agent -p` CLI (Cursor Agent CLI, CURSOR_API_KEY).

CLI usage:
  python agents/groq_agent.py "write a Python sort function"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


def _find_cli() -> str | None:
    for p in [
        shutil.which("agent"),
        os.path.expanduser("~/.local/bin/agent"),
        "/usr/local/bin/agent",
        "/opt/homebrew/bin/agent",
    ]:
        if p and os.path.isfile(p):
            return p
    return None


async def run(prompt: str, model: str = "") -> dict:
    cli = _find_cli()
    if not cli:
        return {"error": "agent CLI not found — install Cursor agent CLI", "agent": "groq", "ok": False}

    api_key = os.getenv("CURSOR_API_KEY", "")
    args = [cli, "-p", prompt]
    if api_key:
        args += ["--api-key", api_key]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "agent CLI timed out", "agent": "groq", "ok": False}

    if proc.returncode != 0:
        return {"error": stderr.decode().strip(), "agent": "groq", "ok": False}

    return {"agent": "groq", "model": model or "cursor-agent", "response": stdout.decode().strip(), "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
