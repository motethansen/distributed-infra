#!/usr/bin/env python3
"""
Codex agent — uses `codex` CLI (OpenAI Codex CLI).
No API key in config — authenticates via `codex login` session.
Must be run from inside a git repository.

CLI usage:
  python agents/codex_agent.py "write a REST API in FastAPI"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


def _find_cli() -> str | None:
    candidates = [
        shutil.which("codex"),
        os.path.expanduser("~/.local/bin/codex"),
        os.path.expanduser("~/.npm-global/bin/codex"),
        os.path.expanduser("~/.npm/bin/codex"),
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
        "/usr/bin/codex",
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
                "codex CLI not found. Install: npm install -g @openai/codex  "
                "then login: codex login"
            ),
            "agent": "codex",
            "ok": False,
        }

    # Codex must run from inside a git repo
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    # --full-auto skips interactive approval prompts so it can run headlessly
    args = [cli, "--approval-mode", "full-auto", "-q", prompt]
    if model:
        args += ["--model", model]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=repo_root,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "codex CLI timed out after 180s", "agent": "codex", "ok": False}

    out = stdout.decode().strip()
    err = stderr.decode().strip()

    if proc.returncode != 0:
        return {"error": err or out, "agent": "codex", "ok": False}

    return {"agent": "codex", "model": model or "codex-default", "response": out, "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
