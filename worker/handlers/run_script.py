"""Handler for run_script tasks."""
from __future__ import annotations

import asyncio

from shared.models import Task
from worker.handlers.common import _detect_action, _run


async def handle_run_script(task: Task) -> dict:
    """
    payload:
      script: str    — shell command/script to run
      cwd: str       — optional working directory
      timeout: int   — seconds (default 120)
    """
    script = task.payload.get("script", "")
    cwd = task.payload.get("cwd") or None
    timeout = int(task.payload.get("timeout", 120))

    if not script:
        return {"needs_human": True, "notes": "No script provided in payload"}

    try:
        rc, out, err = await asyncio.wait_for(_run(script, cwd=cwd), timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "needs_human": True,
            "notes": f"Script timed out after {timeout}s",
            "action": f"Increase 'timeout' in the task payload (current: {timeout}s), or break the task into smaller steps.",
        }

    if rc != 0:
        diagnosis = _detect_action(out, err, rc)
        return {
            "needs_human": True,
            "stdout": out,
            "stderr": err,
            **diagnosis,
        }

    return {"returncode": rc, "stdout": out, "stderr": err}
