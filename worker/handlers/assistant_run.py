"""Handler for `assistant_run` tasks — runs ai_agent_assistant via subprocess.

Thin adapter: ai_agent_assistant stays in its own repo and venv; this handler
shells out to its CLI, captures stdout, and returns it as the task result.

Payload schema:
    {"subcommand": "today" | "sync" | "status" | "plan",
     "args": ""                       # for `plan`, "today" or "week"; ignored otherwise
    }
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from shared.models import Task

ASSISTANT_DIR    = Path(os.getenv(
    "ASSISTANT_PROJECT_DIR",
    "/Users/michaelhansen/Projects/github/ai_agent_assistant",
))
ASSISTANT_PYTHON = ASSISTANT_DIR / "venv" / "bin" / "python"
TIMEOUT_SECS     = 60

# Subcommand → list of CLI flags to append to `main.py`. Allowlist: anything not
# in this dict is rejected before we ever spawn a subprocess.
_SUBCOMMANDS: dict[str, list[str]] = {
    "today":  ["--today"],
    "sync":   ["--sync"],
    "status": ["--status"],
    "plan":   ["--plan"],  # args (today|week) appended at call time
}


async def handle_assistant_run(task: Task) -> dict:
    payload    = task.payload or {}
    subcommand = (payload.get("subcommand") or "").strip().lower()
    args       = (payload.get("args") or "").strip()

    if subcommand not in _SUBCOMMANDS:
        return {
            "error": f"Unknown subcommand: {subcommand!r}. Allowed: {sorted(_SUBCOMMANDS)}",
        }

    if not ASSISTANT_PYTHON.exists():
        return {
            "error": f"Assistant venv not found at {ASSISTANT_PYTHON}. "
                     f"Run install.sh in {ASSISTANT_DIR}.",
            "needs_human": True,
            "notes": "ai_agent_assistant venv missing on this machine",
        }

    cmd = [str(ASSISTANT_PYTHON), "main.py", *_SUBCOMMANDS[subcommand]]
    if subcommand == "plan" and args in ("today", "week"):
        cmd.append(args)

    # NO_COLOR strips ANSI from Rich output so the WhatsApp reply isn't full of
    # control codes. Box-drawing chars still come through; acceptable for v1.
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ASSISTANT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SECS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "error": f"Assistant subprocess timed out after {TIMEOUT_SECS}s",
            "subcommand": subcommand,
        }

    out = (stdout or b"").decode("utf-8", errors="replace").strip()
    err = (stderr or b"").decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        return {
            "error": f"Assistant exited {proc.returncode}",
            "stderr": err[:4000],
            "stdout": out[:4000],
            "subcommand": subcommand,
        }

    # Bridge looks for `response` in result for the WhatsApp reply text.
    return {
        "response": out[:4000],
        "subcommand": subcommand,
        "exit_code": proc.returncode,
    }
