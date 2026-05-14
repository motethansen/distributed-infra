"""Handler for npm_build tasks."""
from __future__ import annotations

import asyncio
import os
import shlex

from shared.models import Task
from worker.handlers.common import _run


async def handle_npm_build(task: Task) -> dict:
    """
    payload:
      project_path: str   — repo root (must have package.json)
      script: str         — npm script to run (default: build)
      install: bool       — run npm ci first (default: false)
      timeout: int        — seconds (default: 600)
    """
    project_path = task.payload.get("project_path", "")
    script = task.payload.get("script", "build")
    install = task.payload.get("install", False)
    timeout = int(task.payload.get("timeout", 600))

    if not project_path or not os.path.isdir(project_path):
        return {"needs_human": True, "notes": f"project_path not found: {project_path}"}

    if install:
        rc, out, err = await asyncio.wait_for(_run("npm ci", cwd=project_path), timeout=120)
        if rc != 0:
            return {"needs_human": True, "notes": f"npm ci failed: {err[-1000:]}"}

    try:
        rc, out, err = await asyncio.wait_for(
            _run(f"npm run {shlex.quote(script)}", cwd=project_path),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"needs_human": True, "notes": f"npm run {script} timed out after {timeout}s"}

    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"npm run {script} failed (exit {rc})",
            "stdout": out[-3000:],
            "stderr": err[-2000:],
        }
    return {"script": script, "stdout": out[-2000:], "status": "build_success"}
