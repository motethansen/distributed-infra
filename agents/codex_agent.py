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
import tempfile

DEFAULT_TIMEOUT_SECS = int(os.environ.get("CODEX_AGENT_TIMEOUT_SECS", "600"))
# codex 0.128 defaults to gpt-5.3-codex, which ChatGPT-account logins reject
# ("model is not supported when using Codex with a ChatGPT account"). Override
# with a model your Codex plan allows via CODEX_AGENT_DEFAULT_MODEL; empty lets
# the CLI pick its own default.
DEFAULT_MODEL = os.environ.get("CODEX_AGENT_DEFAULT_MODEL", "")
_CHATGPT_MODEL_ERR = "is not supported when using Codex with a ChatGPT account"


def _find_node() -> str | None:
    candidates = [
        shutil.which("node"),
        "/usr/local/bin/node",
        "/usr/bin/node",
        "/opt/homebrew/bin/node",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


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


async def run(prompt: str, model: str = "", cwd: str | None = None, timeout: int | None = None) -> dict:
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

    # codex runs best from inside a git repo; default to this repo's root.
    repo_root = os.path.expanduser(cwd) if cwd else os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    # codex is a Node.js script; find node explicitly so it works in subprocess
    # environments where PATH may not include the node directory.
    node = _find_node()
    cmd = [node, cli] if node else [cli]

    chosen_model = model or DEFAULT_MODEL
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECS

    # codex 0.128+: headless runs use the `exec` subcommand (the old top-level
    # --full-auto flag was removed). read-only sandbox keeps a remote-triggered
    # agent from modifying the repo it runs in. -o writes just the final message
    # so the WhatsApp reply isn't cluttered with session headers/reasoning.
    tmp = tempfile.NamedTemporaryFile(prefix="codex_last_", suffix=".txt", delete=False)
    tmp.close()
    last_path = tmp.name
    args = cmd + ["exec", "--skip-git-repo-check", "--sandbox", "read-only",
                  "--color", "never", "-o", last_path]
    if chosen_model:
        args += ["-m", chosen_model]
    args += [prompt]

    # Ensure node directories are in PATH for child processes
    env = os.environ.copy()
    env["PATH"] = "/usr/local/bin:/usr/bin:/opt/homebrew/bin:" + env.get("PATH", "")

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,  # don't block reading stdin in headless runs
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo_root,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": f"codex CLI timed out after {effective_timeout}s", "agent": "codex", "ok": False}

        out = stdout.decode().strip()
        err = stderr.decode().strip()
        try:
            with open(last_path, encoding="utf-8") as f:
                final = f.read().strip()
        except OSError:
            final = ""
    finally:
        try:
            os.unlink(last_path)
        except OSError:
            pass

    # codex prints model/auth errors to stdout or stderr and can still exit non-zero.
    if _CHATGPT_MODEL_ERR in f"{out}\n{err}":
        return {
            "error": (
                "Codex model not supported on this ChatGPT account. Set "
                "CODEX_AGENT_DEFAULT_MODEL to a model your plan allows, or re-run `codex login`."
            ),
            "agent": "codex",
            "ok": False,
        }

    if proc.returncode != 0 and not final:
        return {"error": err or out or "codex exited non-zero", "agent": "codex", "ok": False}

    return {"agent": "codex", "model": chosen_model or "codex-default", "response": final or out, "ok": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
