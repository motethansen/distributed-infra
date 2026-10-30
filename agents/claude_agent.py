#!/usr/bin/env python3
"""
Claude agent — uses `claude -p` CLI (Claude Code, covered by subscription).
No API key needed — authenticates via `claude login` session.

CLI usage:
  python agents/claude_agent.py "write a hello world in Python"
"""
from __future__ import annotations

import asyncio
import json
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
# Haiku (testing / mechanical work). Opus is blocked unless the orchestrator grants it to a
# role in config/roles.yaml (too expensive for general dispatch volume). Override the default with CLAUDE_AGENT_DEFAULT_MODEL.
DEFAULT_MODEL = os.environ.get("CLAUDE_AGENT_DEFAULT_MODEL", "sonnet")
BLOCKED_MODEL_SUBSTRINGS = ("opus",)


def _resolve_model(requested: str, allow_opus: bool = False) -> str | None:
    """Pick the effective model for a call. Returns the alias to pass to claude
    --model, or None if Opus was requested (caller should treat as error).
    `allow_opus` is only ever set by the orchestrator for a role that config/roles.yaml
    permits (see agents/roles.py); a caller cannot grant it through the task payload."""
    chosen = (requested or DEFAULT_MODEL or "").strip().lower()
    if not chosen:
        return ""  # let claude CLI use its own default
    if not allow_opus and any(blocked in chosen for blocked in BLOCKED_MODEL_SUBSTRINGS):
        return None
    return chosen


async def run(prompt: str, model: str = "", cwd: str | None = None, timeout: int | None = None,
              session_id: str = "", resume: bool = False, allow_opus: bool = False) -> dict:
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

    effective_model = _resolve_model(model, allow_opus)
    if effective_model is None:
        return {
            "error": f"opus blocked by cost policy (requested model={model!r}); use 'sonnet' or 'haiku'",
            "agent": "claude",
            "ok": False,
        }

    # --output-format json: same answer, plus the exact token usage and the CLI's cost estimate.
    args = [cli, "-p", prompt, "--dangerously-skip-permissions", "--output-format", "json"]
    # Multi-turn: --session-id pins a caller-supplied id on the first turn;
    # --resume continues that same conversation on later turns.
    if session_id:
        args += (["--resume", session_id] if resume else ["--session-id", session_id])
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
    text, usage, is_error = parse_json_output(out)

    if proc.returncode != 0 or is_error:
        return {"error": err or text, "agent": "claude", "ok": False}

    result = {"agent": "claude", "model": effective_model or "claude-default",
              "response": text, "session_id": session_id, "ok": True}
    if usage:
        result["usage"] = usage
    return result


def parse_json_output(out: str) -> tuple[str, dict | None, bool]:
    """(answer text, usage, is_error) from `claude -p --output-format json`; if the output is not
    that JSON (older CLI, or a plain error), the raw text is the answer and there is no usage."""
    try:
        d = json.loads(out)
    except ValueError:
        return out, None, False
    if not isinstance(d, dict) or "result" not in d:
        return out, None, False
    text = d.get("result")
    text = text.strip() if isinstance(text, str) else out
    u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
    usage = None
    if u:
        tin = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        usage = {"tokens_in": tin, "tokens_out": int(u.get("output_tokens") or 0)}
        if d.get("total_cost_usd") is not None:
            usage["cost_usd"] = d["total_cost_usd"]   # the CLI's estimate; a subscription is not billed per token
    return text, usage, bool(d.get("is_error"))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
