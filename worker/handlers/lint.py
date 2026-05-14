"""Handler for lint tasks."""
from __future__ import annotations

import os

from shared.models import Task
from worker.handlers.common import _run


async def handle_lint(task: Task) -> dict:
    """
    payload:
      project_path: str   — repo root
      tool: str           — ruff | eslint | ktlint | swiftlint (default: ruff)
      fix: bool           — auto-fix if supported (default: false)
      args: str           — extra args
    """
    project_path = task.payload.get("project_path", "")
    tool = task.payload.get("tool", "ruff")
    fix = task.payload.get("fix", False)
    args = task.payload.get("args", "")

    FIX_FLAG = {"ruff": "--fix", "eslint": "--fix", "ktlint": "-F", "swiftlint": "--fix"}
    fix_flag = FIX_FLAG.get(tool, "") if fix else ""

    CMDS = {
        "ruff":      f"ruff check {fix_flag} {args} .",
        "eslint":    f"npx eslint {fix_flag} {args} .",
        "ktlint":    f"ktlint {fix_flag} {args}",
        "swiftlint": f"swiftlint {fix_flag} {args}",
    }
    cmd = CMDS.get(tool, f"{tool} {args}")

    if not project_path or not os.path.isdir(project_path):
        return {"needs_human": True, "notes": f"project_path not found: {project_path}"}

    rc, out, err = await _run(cmd, cwd=project_path)
    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"{tool} found issues (exit {rc})",
            "stdout": out[-3000:],
            "stderr": err[-1000:],
        }
    return {"tool": tool, "stdout": out, "status": "lint_passed"}
