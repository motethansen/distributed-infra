#!/usr/bin/env python3
"""
Cursor Agent — uses `agent -p` CLI (Cursor Agent CLI).
No API key needed — authenticates via `agent login` session.

CLI usage:
  python agents/groq_agent.py "write a Python sort function"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


def _find_cli() -> str | None:
    candidates = [
        shutil.which("agent"),
        os.path.expanduser("~/.local/bin/agent"),
        os.path.expanduser("~/.npm-global/bin/agent"),
        os.path.expanduser("~/.npm/bin/agent"),
        "/usr/local/bin/agent",
        "/opt/homebrew/bin/agent",
        "/usr/bin/agent",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


async def run(prompt: str, model: str = "") -> dict:
    cli = _find_cli()
    if not cli:
        return {
            "error": (
                "Cursor agent CLI not found. Install Cursor app, then: agent login"
            ),
            "agent": "groq",
            "ok": False,
        }

    args = [cli, "-p", prompt, "--trust"]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "agent CLI timed out after 120s", "agent": "groq", "ok": False}

    out = stdout.decode().strip()
    err = stderr.decode().strip()

    if proc.returncode != 0:
        return {"error": err or out, "agent": "groq", "ok": False}

    return {"agent": "groq", "model": model or "cursor-agent", "response": out, "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
