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


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Kill the CLI and reap it, so a timeout or a cancelled caller never leaves a model process running."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except Exception:
        pass


async def run(prompt: str, model: str = "", cwd: str | None = None, timeout: int | None = None,
              session_id: str = "", resume: bool = False, allow_opus: bool = False, tools_off: bool = False) -> dict:
    # The cost policy is checked before the CLI is looked up: refusing Opus must not depend on what is installed
    # (on a machine without the CLI the caller would otherwise be told "not found" and retry elsewhere).
    effective_model = _resolve_model(model, allow_opus)
    if effective_model is None:
        return {
            "error": f"opus blocked by cost policy (requested model={model!r}); use 'sonnet' or 'haiku'",
            "agent": "claude",
            "ok": False,
        }

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

    # --output-format json: same answer, plus the exact token usage and the CLI's cost estimate.
    if tools_off:
        # for prompts that carry untrusted text (backlog evidence): no tools at all and no permission bypass, so nothing
        # in the text can make the CLI run a command or touch a file. `--tools ""` removes the built-in tools only; the
        # account's connectors (Gmail, Calendar, Drive ...) are MCP tools and stay unless --strict-mcp-config drops them.
        args = [cli, "-p", prompt, "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--output-format", "json"]
    else:
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
        await _stop(proc)
        return {"error": f"claude CLI timed out after {effective_timeout}s", "agent": "claude", "ok": False}
    except BaseException:        # the caller was cancelled (an outer timeout): do not leave the CLI running
        await _stop(proc)
        raise

    out = stdout.decode().strip()
    err = stderr.decode().strip()
    text, usage, is_error = parse_json_output(out)

    if proc.returncode != 0 or is_error:
        failed = {"error": err or text, "agent": "claude", "ok": False}
        if _has_usage(usage):
            failed["usage"] = usage   # what the CLI printed is still spent, even if the task failed
        return failed

    result = {"agent": "claude", "model": effective_model or "claude-default",
              "response": text, "session_id": session_id, "ok": True}
    if _has_usage(usage):
        result["usage"] = usage
    return result


USAGE_KEYS = ("tokens_in", "tokens_cache_read", "tokens_cache_write", "tokens_out", "cost_usd", "model", "agent")


def _count(v) -> int | None:
    """A token count, or None when the CLI did not print one (missing is not 0)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v) if v >= 0 else None


def _model_of(d: dict) -> str | None:
    mu = d.get("modelUsage")
    if isinstance(mu, dict) and mu:
        def spend(k):
            v = mu.get(k)
            return (v.get("costUSD") or 0) if isinstance(v, dict) and isinstance(v.get("costUSD"), (int, float)) else 0
        return str(max(mu, key=spend))[:80]   # the model that cost most (a run can also use a small helper model)
    m = d.get("model")
    return m[:80] if isinstance(m, str) and m else None


def parse_json_output(out: str) -> tuple[str, dict, bool]:
    """(answer text, usage, is_error) from `claude -p --output-format json`.

    usage always has every key of USAGE_KEYS: tokens_in (usage.input_tokens, uncached only),
    tokens_cache_read, tokens_cache_write, tokens_out, cost_usd (total_cost_usd), model, agent='claude'.
    A key the CLI did not print is None, never 0. If the output is not that JSON (older CLI, plain
    text, a plain error) the raw text is the answer and every usage field except agent is None.
    Never raises. Usage is returned even when is_error is true."""
    usage: dict = {k: None for k in USAGE_KEYS}
    usage["agent"] = "claude"
    try:
        d = json.loads(out)
    except (ValueError, TypeError):
        return out, usage, False
    if not isinstance(d, dict) or "result" not in d:
        return out, usage, False
    text = d.get("result")
    text = text.strip() if isinstance(text, str) else out
    u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
    usage["tokens_in"] = _count(u.get("input_tokens"))
    usage["tokens_cache_read"] = _count(u.get("cache_read_input_tokens"))
    usage["tokens_cache_write"] = _count(u.get("cache_creation_input_tokens"))
    usage["tokens_out"] = _count(u.get("output_tokens"))
    cost = d.get("total_cost_usd")   # the CLI's estimate; a subscription is not billed per token
    usage["cost_usd"] = cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    usage["model"] = _model_of(d)
    return text, usage, bool(d.get("is_error"))


def _has_usage(usage: dict) -> bool:
    return any(usage.get(k) is not None for k in USAGE_KEYS if k not in ("agent", "model"))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
