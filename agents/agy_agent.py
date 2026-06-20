#!/usr/bin/env python3
"""
Antigravity agent — uses `agy -p` CLI (Google Antigravity CLI).
No API key needed — authenticates via `agy login` session.

CLI usage:
  python agents/agy_agent.py "explain async/await in Python"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


def _find_cli() -> str | None:
    candidates = [
        shutil.which("agy"),
        os.path.expanduser("~/.local/bin/agy"),
        "/usr/local/bin/agy",
        "/opt/homebrew/bin/agy",
        "/usr/bin/agy",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


DEFAULT_TIMEOUT_SECS = int(os.environ.get("AGY_AGENT_TIMEOUT_SECS", "1800"))


async def run(prompt: str, model: str = "", cwd: str | None = None, timeout: int | None = None) -> dict:
    cli = _find_cli()
    if not cli:
        return {
            "error": (
                "agy CLI not found. Install: brew install --cask antigravity-cli  "
                "then login: agy login"
            ),
            "agent": "agy",
            "ok": False,
        }

    # --dangerously-skip-permissions skips interactive confirmations so agy runs headlessly
    args = [cli, "--dangerously-skip-permissions", "-p", prompt]
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
        return {"error": f"agy CLI timed out after {effective_timeout}s", "agent": "agy", "ok": False}

    out = stdout.decode().strip()
    err = stderr.decode().strip()

    if proc.returncode != 0:
        return {"error": err or out, "agent": "agy", "ok": False}

    return {"agent": "agy", "model": model or "agy-default", "response": out, "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
