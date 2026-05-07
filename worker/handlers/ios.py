"""iOS build handler — runs on Mac Mini."""
from __future__ import annotations

import asyncio
import shlex

from shared.models import Task
from worker.handlers.common import _run


async def handle_ios_build(task: Task) -> dict:
    """
    payload:
      project_path: str      — path to the .xcodeproj or .xcworkspace parent dir
      scheme: str            — Xcode scheme name
      workspace: str         — .xcworkspace filename (optional; uses project if absent)
      destination: str       — e.g. "generic/platform=iOS Simulator,name=iPhone 15"
      action: str            — build | test | archive (default: build)
      clean: bool            — run clean first (default false)
    """
    project_path = task.payload.get("project_path", "")
    scheme = task.payload.get("scheme", "")
    workspace = task.payload.get("workspace", "")
    destination = task.payload.get("destination", "generic/platform=iOS Simulator,name=iPhone 15")
    action = task.payload.get("action", "build")
    clean = task.payload.get("clean", False)

    if not project_path or not scheme:
        return {"needs_human": True, "notes": "project_path and scheme are required in payload"}

    proj_flag = f"-workspace {shlex.quote(workspace)}" if workspace else f"-project *.xcodeproj"

    clean_action = "clean " if clean else ""
    cmd = (
        f"xcodebuild {proj_flag} "
        f"-scheme {shlex.quote(scheme)} "
        f"-destination {shlex.quote(destination)} "
        f"{clean_action}{action}"
    )

    try:
        rc, out, err = await asyncio.wait_for(
            _run(cmd, cwd=project_path),
            timeout=1800,  # 30 min for archive
        )
    except asyncio.TimeoutError:
        return {"needs_human": True, "notes": f"xcodebuild timed out after 30 min"}

    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"xcodebuild {action} failed (exit {rc})",
            "stdout": out[-3000:],
            "stderr": err[-3000:],
        }

    return {
        "action": action,
        "scheme": scheme,
        "stdout": out[-2000:],
        "status": "build_success",
    }
