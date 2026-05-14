"""
Handler registry — each machine loads only the handlers it supports.
CAPABILITIES is read from machines.yaml via MACHINE_CAPABILITIES env var.

Custom skills created via `skills create <name>` are auto-discovered: any
file worker/handlers/<name>.py that exports handle_<name>(task) is picked up
when a task arrives with type == <name>.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

from shared.models import Task

# Comma-separated list set in .env: e.g. "android_build,git_pull,run_script"
CAPABILITIES: list[str] = [
    c.strip() for c in os.getenv("MACHINE_CAPABILITIES", "git_pull,run_script").split(",") if c.strip()
]

_HANDLERS_DIR = Path(__file__).parent


async def dispatch(task: Task) -> dict:
    """Route a task to the correct handler via auto-discovery.

    Looks for worker/handlers/<task_type>.py with a handle_<task_type>(task)
    function. Falls back to needs_human for unknown types.
    """
    handler_file = _HANDLERS_DIR / f"{task.type}.py"
    if handler_file.exists():
        try:
            module = importlib.import_module(f"worker.handlers.{task.type}")
            fn = getattr(module, f"handle_{task.type}", None)
            if callable(fn):
                return await fn(task)
        except Exception as exc:
            return {
                "needs_human": True,
                "notes": f"Handler for '{task.type}' raised: {exc}",
            }

    return {
        "error": f"No handler for task type: {task.type}",
        "needs_human": True,
        "notes": f"Unhandled type: {task.type}",
    }
