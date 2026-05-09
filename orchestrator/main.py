"""Orchestrator queue server — runs on MacBook Pro."""
from __future__ import annotations

import os
import subprocess
from typing import Any

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import JSONResponse

from shared.models import ClaimRequest, Task, TaskCreate, TaskStatus, TaskUpdate
from orchestrator import db

SECRET_KEY = os.getenv("SECRET_KEY", "")
MACHINE_NAME = os.getenv("MACHINE_NAME", "orchestrator")

app = FastAPI(title="Distributed Infra Queue", version="0.1.0")


def _check_auth(x_secret_key: str = "") -> None:
    if SECRET_KEY and x_secret_key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid secret key")


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "machine": MACHINE_NAME, "role": "orchestrator"}


# ── Task CRUD ────────────────────────────────────────────────────────────────

@app.post("/tasks", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    task = Task(
        type=body.type,
        priority=body.priority,
        payload=body.payload,
        notes=body.notes,
        created_by=MACHINE_NAME,
    )
    return await db.insert_task(task)


@app.get("/tasks", response_model=list[Task])
async def list_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    x_secret_key: str = Header(default=""),
):
    _check_auth(x_secret_key)
    return await db.list_tasks(status=status, limit=limit)


@app.get("/tasks/needs-human", response_model=list[Task])
async def tasks_needing_human(x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    return await db.list_tasks(status=TaskStatus.needs_human)


@app.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: str, x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.patch("/tasks/{task_id}", response_model=Task)
async def update_task(task_id: str, body: TaskUpdate, x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    fields: dict[str, Any] = body.model_dump(exclude_none=True)
    task = await db.update_task(task_id, fields)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ── Worker queue operations ──────────────────────────────────────────────────

@app.post("/tasks/claim", response_model=Task | None)
async def claim_task(body: ClaimRequest, x_secret_key: str = Header(default="")):
    """Worker calls this to atomically claim the next available task."""
    _check_auth(x_secret_key)
    task = await db.claim_next_task(body.worker_name, body.capabilities)
    if not task:
        return JSONResponse(status_code=204, content=None)
    return task


@app.post("/tasks/{task_id}/complete", response_model=Task)
async def complete_task(task_id: str, result: dict = {}, x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    task = await db.update_task(task_id, {"status": TaskStatus.done, "result": result})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/tasks/{task_id}/fail", response_model=Task)
async def fail_task(task_id: str, result: dict = {}, x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    task = await db.update_task(task_id, {"status": TaskStatus.failed, "result": result})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/tasks/{task_id}/needs-human", response_model=Task)
async def escalate_task(
    task_id: str,
    notes: str = "",
    action: str = "",
    x_secret_key: str = Header(default=""),
):
    """Worker escalates a task that requires human decision."""
    _check_auth(x_secret_key)

    # Combine notes + action into the stored notes field so it survives without
    # a schema change.  Format: "<notes> | ACTION: <action>"
    full_notes = notes
    if action:
        full_notes = f"{notes} | ACTION: {action}" if notes else f"ACTION: {action}"

    task = await db.update_task(task_id, {"status": TaskStatus.needs_human, "notes": full_notes})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # ── macOS notification on the orchestrator machine ───────────────────────
    _notify(
        title="⚠️ Task needs your attention",
        subtitle=f"{task_id[:8]}  ·  {(task.payload or {}).get('type', task.type)}",
        message=action or notes or "A task requires human action — run: da › review",
    )

    return task


def _notify(title: str, subtitle: str, message: str) -> None:
    """Fire a macOS notification. Silently ignored on non-macOS hosts."""
    try:
        # Escape double quotes for AppleScript
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')[:200]

        script = (
            f'display notification "{esc(message)}" '
            f'with title "{esc(title)}" '
            f'subtitle "{esc(subtitle)}"'
        )
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass  # not macOS
