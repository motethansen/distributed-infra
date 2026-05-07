#!/usr/bin/env python3
"""
Codex agent — OpenAI gpt-4o / Codex via OPENAI_API_KEY.
Also tries the `codex` CLI if installed (OpenAI Codex CLI tool).

CLI usage:
  python agents/codex_agent.py "write a REST API in FastAPI"
  python agents/codex_agent.py "fix this bug" --model gpt-4o
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys


async def run(prompt: str, model: str = "gpt-4o") -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "")

    # Prefer the npm-installed codex (newer) over any system codex
    codex_cli = (
        os.path.expanduser("~/.npm-global/bin/codex")
        if os.path.isfile(os.path.expanduser("~/.npm-global/bin/codex"))
        else shutil.which("codex")
    )
    if codex_cli and not api_key:
        return await _run_cli(prompt, codex_cli)

    if not api_key:
        return {
            "error": "OPENAI_API_KEY not set and codex CLI not found — add OPENAI_API_KEY to .env",
            "agent": "codex",
            "ok": False,
        }

    return await _run_sdk(prompt, model, api_key)


async def _run_sdk(prompt: str, model: str, api_key: str) -> dict:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        text = completion.choices[0].message.content
        return {"agent": "codex", "model": model, "response": text, "ok": True}
    except Exception as exc:
        return {"error": str(exc), "agent": "codex", "ok": False}


async def _run_cli(prompt: str, cli_path: str) -> dict:
    # Must run from a git repo dir; use the repo root this file lives in
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    proc = await asyncio.create_subprocess_exec(
        cli_path, "exec", prompt,
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
    return {"agent": "codex", "model": "codex-cli", "response": stdout.decode().strip(), "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="gpt-4o")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
