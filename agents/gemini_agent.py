#!/usr/bin/env python3
"""
Gemini agent — uses `gemini -p` CLI (Google Gemini CLI).
No API key needed — authenticates via `gemini login` session.

CLI usage:
  python agents/gemini_agent.py "explain async/await in Python"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


def _find_cli() -> str | None:
    candidates = [
        shutil.which("gemini"),
        os.path.expanduser("~/.local/bin/gemini"),
        os.path.expanduser("~/.npm-global/bin/gemini"),
        os.path.expanduser("~/.npm/bin/gemini"),
        "/usr/local/bin/gemini",
        "/opt/homebrew/bin/gemini",
        "/usr/bin/gemini",
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
                "gemini CLI not found. Install: npm install -g @google/gemini-cli  "
                "then login: gemini login"
            ),
            "agent": "gemini",
            "ok": False,
        }

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
        return {"error": "gemini CLI timed out after 120s", "agent": "gemini", "ok": False}

    out = stdout.decode().strip()
    err = stderr.decode().strip()

    if proc.returncode != 0:
        return {"error": err or out, "agent": "gemini", "ok": False}

    return {"agent": "gemini", "model": model or "gemini-default", "response": out, "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
