"""Handlers available on all worker machines."""
from __future__ import annotations

import asyncio
import os
import shlex

from shared.models import Task


async def _run(cmd: str, cwd: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def handle_git_pull(task: Task) -> dict:
    """
    payload:
      repo_path: str   — absolute path to the local git repo
      branch: str      — optional branch to checkout first
    """
    repo_path = task.payload.get("repo_path", "")
    branch = task.payload.get("branch", "")

    if not repo_path or not os.path.isdir(repo_path):
        return {"needs_human": True, "notes": f"repo_path not found: {repo_path}"}

    if branch:
        rc, out, err = await _run(f"git checkout {shlex.quote(branch)}", cwd=repo_path)
        if rc != 0:
            return {"needs_human": True, "notes": f"checkout failed: {err}"}

    rc, out, err = await _run("git pull --ff-only", cwd=repo_path)
    if rc != 0:
        return {"needs_human": True, "notes": f"git pull failed: {err}"}

    return {"stdout": out.strip(), "branch": branch or "current"}


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
        return {"needs_human": True, "notes": f"Script timed out after {timeout}s"}

    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"Script exited {rc}",
            "stdout": out,
            "stderr": err,
        }

    return {"returncode": rc, "stdout": out, "stderr": err}
