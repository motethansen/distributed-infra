"""Android build handler — runs on ThinkPad (Ubuntu)."""
from __future__ import annotations

import asyncio
import os

from shared.models import Task
from worker.handlers.common import _run


async def handle_android_build(task: Task) -> dict:
    """
    payload:
      project_path: str   — path to Android project root (has gradlew)
      variant: str        — e.g. "assembleDebug" | "assembleRelease" | "bundleRelease"
      clean: bool         — run `./gradlew clean` first (default false)
    """
    project_path = task.payload.get("project_path", "")
    variant = task.payload.get("variant", "assembleDebug")
    clean = task.payload.get("clean", False)

    gradlew = os.path.join(project_path, "gradlew")
    if not os.path.isfile(gradlew):
        return {"needs_human": True, "notes": f"gradlew not found at {gradlew}"}

    if clean:
        rc, out, err = await _run("./gradlew clean", cwd=project_path)
        if rc != 0:
            return {"needs_human": True, "notes": f"gradle clean failed: {err}"}

    rc, out, err = await asyncio.wait_for(
        _run(f"./gradlew {variant}", cwd=project_path),
        timeout=900,  # 15 min
    )

    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"gradle {variant} failed (exit {rc})",
            "stdout": out[-3000:],
            "stderr": err[-3000:],
        }

    return {
        "variant": variant,
        "stdout": out[-2000:],
        "status": "build_success",
    }
