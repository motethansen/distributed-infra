#!/usr/bin/env python3
"""
Gemini agent — uses `gemini -p` CLI (Google Gemini CLI).

CLI usage:
  python agents/gemini_agent.py "explain async/await in Python"
  python agents/gemini_agent.py "review this code" --model gemini-2.5-flash
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


def _find_cli() -> str | None:
    for p in [
        shutil.which("gemini"),
        "/opt/homebrew/bin/gemini",
        "/usr/local/bin/gemini",
        "/usr/bin/gemini",
    ]:
        if p and os.path.isfile(p):
            return p
    return None


async def run(prompt: str, model: str = "") -> dict:
    cli = _find_cli()
    if not cli:
        return {"error": "gemini CLI not found — install from https://github.com/google-gemini/gemini-cli", "agent": "gemini", "ok": False}

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
        return {"error": "gemini CLI timed out", "agent": "gemini", "ok": False}

    if proc.returncode != 0:
        return {"error": stderr.decode().strip(), "agent": "gemini", "ok": False}

    return {"agent": "gemini", "model": model or "gemini-default", "response": stdout.decode().strip(), "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
