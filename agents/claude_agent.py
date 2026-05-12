#!/usr/bin/env python3
"""
Claude agent — uses `claude -p` CLI (Claude Code, covered by subscription).
No API key needed — authenticates via `claude login` session.

CLI usage:
  python agents/claude_agent.py "write a hello world in Python"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


def _find_cli() -> str | None:
    candidates = [
        shutil.which("claude"),
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.npm-global/bin/claude"),
        os.path.expanduser("~/.npm/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        "/usr/bin/claude",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


DEFAULT_TIMEOUT_SECS = int(os.environ.get("CLAUDE_AGENT_TIMEOUT_SECS", "1800"))


async def run(prompt: str, model: str = "", cwd: str | None = None, timeout: int | None = None) -> dict:
    cli = _find_cli()
    if not cli:
        return {
            "error": (
                "claude CLI not found. Install: npm install -g @anthropic-ai/claude-code  "
                "then login: claude login"
            ),
            "agent": "claude",
            "ok": False,
        }

    args = [cli, "-p", prompt, "--dangerously-skip-permissions"]
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
        return {"error": f"claude CLI timed out after {effective_timeout}s", "agent": "claude", "ok": False}

    out = stdout.decode().strip()
    err = stderr.decode().strip()

    if proc.returncode != 0:
        return {"error": err or out, "agent": "claude", "ok": False}

    return {"agent": "claude", "model": model or "claude-default", "response": out, "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
