"""Handler for git_pull tasks."""
from __future__ import annotations

import os
import shlex

from shared.models import Task
from worker.handlers.common import _run


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
