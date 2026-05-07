#!/usr/bin/env python3
"""
Codex agent — uses `codex exec` CLI (OpenAI Codex CLI).

CLI usage:
  python agents/codex_agent.py "write a REST API in FastAPI"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


def _find_cli() -> str | None:
    for p in [
        shutil.which("codex"),
        os.path.expanduser("~/.npm-global/bin/codex"),
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
        "/usr/bin/codex",
    ]:
        if p and os.path.isfile(p):
            return p
    return None


async def run(prompt: str, model: str = "") -> dict:
    cli = _find_cli()
    if not cli:
        return {"error": "codex CLI not found — run: npm install -g @openai/codex", "agent": "codex", "ok": False}

    # Must run from a git repo directory
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    args = [cli, "exec", prompt]
    if model:
        args += ["-m", model]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=repo_root,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "codex CLI timed out", "agent": "codex", "ok": False}

    if proc.returncode != 0:
        return {"error": stderr.decode().strip(), "agent": "codex", "ok": False}

    return {"agent": "codex", "model": model or "codex-default", "response": stdout.decode().strip(), "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
