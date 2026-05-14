"""Handler for test_run tasks."""
from __future__ import annotations

import asyncio
import os

from shared.models import Task
from worker.handlers.common import _run


async def handle_test_run(task: Task) -> dict:
    """
    payload:
      project_path: str   — repo root
      runner: str         — pytest | jest | gradle_test | xcode_test (default: pytest)
      args: str           — extra args passed to the runner
      timeout: int        — seconds (default 300)
    """
    project_path = task.payload.get("project_path", "")
    runner = task.payload.get("runner", "pytest")
    args = task.payload.get("args", "")
    timeout = int(task.payload.get("timeout", 300))

    RUNNERS = {
        "pytest":        f"python -m pytest {args} --tb=short -q",
        "jest":          f"npx jest {args} --ci",
        "gradle_test":   f"./gradlew test {args}",
        "xcode_test":    f"xcodebuild test {args}",
    }
    cmd = RUNNERS.get(runner, f"{runner} {args}")

    if not project_path or not os.path.isdir(project_path):
        return {"needs_human": True, "notes": f"project_path not found: {project_path}"}

    try:
        rc, out, err = await asyncio.wait_for(_run(cmd, cwd=project_path), timeout=timeout)
    except asyncio.TimeoutError:
        return {"needs_human": True, "notes": f"Tests timed out after {timeout}s"}

    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"{runner} exited {rc} — tests may be failing",
            "stdout": out[-3000:],
            "stderr": err[-2000:],
        }
    return {"runner": runner, "stdout": out[-3000:], "status": "tests_passed"}
