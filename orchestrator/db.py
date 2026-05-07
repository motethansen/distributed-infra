"""SQLite-backed task queue. Async-safe via aiosqlite."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import aiosqlite

from shared.models import Task, TaskStatus

DB_PATH = os.getenv("QUEUE_DB_PATH", "./data/queue.db")

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    priority    INTEGER NOT NULL DEFAULT 5,
    payload     TEXT NOT NULL DEFAULT '{}',
    created_by  TEXT NOT NULL,
    assigned_to TEXT,
    result      TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def _db_path() -> str:
    path = DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def _row_to_task(row: aiosqlite.Row) -> Task:
    d = dict(row)
    d["payload"] = json.loads(d["payload"] or "{}")
    d["result"] = json.loads(d["result"]) if d["result"] else None
    d["created_at"] = datetime.fromisoformat(d["created_at"])
    d["updated_at"] = datetime.fromisoformat(d["updated_at"])
    return Task(**d)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(CREATE_TABLE)
    await db.commit()


async def insert_task(task: Task) -> Task:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        await db.execute(
            """INSERT INTO tasks (id,type,status,priority,payload,created_by,assigned_to,result,notes,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task.id, task.type, task.status, task.priority,
                json.dumps(task.payload), task.created_by, task.assigned_to,
                json.dumps(task.result) if task.result else None,
                task.notes,
                task.created_at.isoformat(), task.updated_at.isoformat(),
            ),
        )
        await db.commit()
    return task


async def get_task(task_id: str) -> Task | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        async with db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
    return _row_to_task(row) if row else None


async def list_tasks(status: str | None = None, limit: int = 100) -> list[Task]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        if status:
            async with db.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY priority DESC, created_at ASC LIMIT ?",
                (status, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM tasks ORDER BY priority DESC, created_at ASC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
    return [_row_to_task(r) for r in rows]


async def claim_next_task(worker_name: str, capabilities: list[str]) -> Task | None:
    """Atomically claim the highest-priority pending task the worker can handle."""
    if not capabilities:
        return None
    cap_placeholders = ",".join("?" * len(capabilities))
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        async with db.execute(
            f"""SELECT * FROM tasks
                WHERE status='pending' AND type IN ({cap_placeholders})
                ORDER BY priority DESC, created_at ASC
                LIMIT 1""",
            capabilities,
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        now = datetime.utcnow().isoformat()
        await db.execute(
            "UPDATE tasks SET status='claimed', assigned_to=?, updated_at=? WHERE id=? AND status='pending'",
            (worker_name, now, row["id"]),
        )
        await db.commit()
        async with db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)) as cur:
            updated = await cur.fetchone()
    return _row_to_task(updated) if updated else None


async def update_task(task_id: str, fields: dict[str, Any]) -> Task | None:
    if not fields:
        return await get_task(task_id)
    fields = dict(fields)
    fields["updated_at"] = datetime.utcnow().isoformat()
    if "result" in fields and fields["result"] is not None:
        fields["result"] = json.dumps(fields["result"])
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [task_id]
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        await db.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", values)
        await db.commit()
    return await get_task(task_id)
