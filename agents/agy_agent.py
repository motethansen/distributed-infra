#!/usr/bin/env python3
"""
Antigravity agent — uses `agy -p` CLI (Google Antigravity CLI).
No API key needed — authenticates via `agy login` session.

CLI usage:
  python agents/agy_agent.py "explain async/await in Python"
"""
from __future__ import annotations

import asyncio
import json
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
    args = [cli, "--dangerously-skip-permissions", "--output-format", "json", "-p", prompt]
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
    text, usage, failed = parse_json_output(out)

    if proc.returncode != 0 or failed:
        failed = {"error": err or text, "agent": "agy", "ok": False}
        if usage:
            failed["usage"] = usage   # printed tokens are spent even when the task failed
        return failed

    result = {"agent": "agy", "model": model or "agy-default", "response": text, "ok": True}
    if usage:
        result["usage"] = usage
    return result


def _count(v) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v) if v >= 0 else None


def _cost(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_json_output(out: str) -> tuple[str, dict | None, bool]:
    """(answer text, usage, failed) from `agy --output-format json`; raw text if it is not that JSON."""
    try:
        d = json.loads(out)
    except ValueError:
        return out, None, False
    if not isinstance(d, dict) or "response" not in d:
        return out, None, False
    text = d.get("response")
    text = text.strip() if isinstance(text, str) else out
    u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
    usage = None
    if u:
        usage = {k: v for k, v in (("tokens_in", _count(u.get("input_tokens"))),
                                   ("tokens_out", _count(u.get("output_tokens"))),
                                   ("tokens_total", _count(u.get("total_tokens"))),   # includes thinking tokens
                                   ("cost_usd", _cost(u.get("cost_usd", d.get("cost_usd")))))   # only if agy prints one
                 if v is not None} or None
    status = str(d.get("status") or "SUCCESS").upper()
    return text, usage, status not in ("SUCCESS", "OK", "COMPLETED")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
