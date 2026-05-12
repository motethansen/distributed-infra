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


DEFAULT_TIMEOUT_SECS = int(os.environ.get("GEMINI_AGENT_TIMEOUT_SECS", "1800"))


async def run(prompt: str, model: str = "", cwd: str | None = None, timeout: int | None = None) -> dict:
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

    # --yolo skips all interactive confirmations so Gemini runs fully headlessly
    args = [cli, "--yolo", "-p", prompt]
    if model:
        args += ["--model", model]

    work_dir = os.path.expanduser(cwd) if cwd else None
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECS

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": f"gemini CLI timed out after {effective_timeout}s", "agent": "gemini", "ok": False}

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
