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

# Cost policy: default to Sonnet (coding/dev planning); allow per-call override to
# Haiku (testing / mechanical work). Opus is blocked entirely — too expensive for
# our dispatch volume. Override the default with CLAUDE_AGENT_DEFAULT_MODEL.
DEFAULT_MODEL = os.environ.get("CLAUDE_AGENT_DEFAULT_MODEL", "sonnet")
BLOCKED_MODEL_SUBSTRINGS = ("opus",)


def _resolve_model(requested: str) -> str | None:
    """Pick the effective model for a call. Returns the alias to pass to claude
    --model, or None if Opus was requested (caller should treat as error)."""
    chosen = (requested or DEFAULT_MODEL or "").strip().lower()
    if not chosen:
        return ""  # let claude CLI use its own default
    if any(blocked in chosen for blocked in BLOCKED_MODEL_SUBSTRINGS):
        return None
    return chosen


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

    effective_model = _resolve_model(model)
    if effective_model is None:
        return {
            "error": f"opus blocked by cost policy (requested model={model!r}); use 'sonnet' or 'haiku'",
            "agent": "claude",
            "ok": False,
        }

    args = [cli, "-p", prompt, "--dangerously-skip-permissions"]
    if effective_model:
        args += ["--model", effective_model]

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

    return {"agent": "claude", "model": effective_model or "claude-default", "response": out, "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
