"""
Handler registry — each machine loads only the handlers it supports.
CAPABILITIES is read from machines.yaml via MACHINE_CAPABILITIES env var.
"""
from __future__ import annotations

import os
from shared.models import Task

# Comma-separated list set in .env: e.g. "android_build,git_pull,run_script"
CAPABILITIES: list[str] = [
    c.strip() for c in os.getenv("MACHINE_CAPABILITIES", "git_pull,run_script").split(",") if c.strip()
]


async def dispatch(task: Task) -> dict:
    """Route a task to the correct handler."""
    from worker.handlers.common import (
        handle_git_pull,
        handle_lint,
        handle_npm_build,
        handle_run_script,
        handle_test_run,
    )

    if task.type == "git_pull":
        return await handle_git_pull(task)
    if task.type == "run_script":
        return await handle_run_script(task)
    if task.type == "test_run":
        return await handle_test_run(task)
    if task.type == "lint":
        return await handle_lint(task)
    if task.type == "npm_build":
        return await handle_npm_build(task)

    if task.type == "agent_run":
        from worker.handlers.agent import handle_agent_run
        return await handle_agent_run(task)

    if task.type == "android_build":
        from worker.handlers.android import handle_android_build
        return await handle_android_build(task)

    if task.type == "ios_build":
        from worker.handlers.ios import handle_ios_build
        return await handle_ios_build(task)

    return {"error": f"No handler for task type: {task.type}", "needs_human": True, "notes": f"Unhandled type: {task.type}"}
