"""SQLite-backed task queue. Async-safe via aiosqlite."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import aiosqlite

from shared import tracking
from shared.models import Task, TaskStatus

DB_PATH = os.getenv("QUEUE_DB_PATH", "./data/queue.db")

# Workload placement (#5b): a task may carry a SOFT preference (_preferred_machine,
# distinct from the hard _target_machine pin). The preferred worker can claim it
# immediately; other capable workers may only claim it after it has been pending
# this many seconds (overflow / failover). 0 disables the wait.
OVERFLOW_GRACE_SECS = float(os.getenv("OVERFLOW_GRACE_SECS", "20"))

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


_MIGRATED: set[str] = set()   # db paths whose tracking columns are known to exist


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(CREATE_TABLE)
    path = _db_path()
    if path not in _MIGRATED:
        cur = await db.execute("PRAGMA table_info(tasks)")
        have = {r[1] for r in await cur.fetchall()}
        for col, typ in tracking.TRACK_COLUMNS.items():
            if col not in have:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
        # Backfill from data the rows already hold (only where still empty).
        for col in ("agent", "model"):
            await db.execute(f"UPDATE tasks SET {col}=json_extract(result,'$.{col}') WHERE {col} IS NULL "
                             f"AND result IS NOT NULL AND json_valid(result) AND json_extract(result,'$.{col}') IS NOT NULL")
        for col in ("project", "sprint", "story"):
            await db.execute(f"UPDATE tasks SET {col}=json_extract(payload,'$.{col}') WHERE {col} IS NULL "
                             f"AND json_valid(payload) AND json_extract(payload,'$.{col}') IS NOT NULL")
        await db.execute("UPDATE tasks SET finished_at=updated_at WHERE finished_at IS NULL "
                         "AND status IN ('done','failed','needs_human')")
        _MIGRATED.add(path)
    await db.commit()


async def insert_task(task: Task) -> Task:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        tags = tracking.from_payload(task.payload)
        await db.execute(
            """INSERT INTO tasks (id,type,status,priority,payload,created_by,assigned_to,result,notes,created_at,updated_at,
                                  project,sprint,story)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task.id, task.type, task.status, task.priority,
                json.dumps(task.payload), task.created_by, task.assigned_to,
                json.dumps(task.result) if task.result else None,
                task.notes,
                task.created_at.isoformat(), task.updated_at.isoformat(),
                tags.get("project"), tags.get("sprint"), tags.get("story"),
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
    # Display/inspection query — ordered by recency (newest first) so `status`/
    # `queue` views surface current activity. Do NOT order by priority here: that
    # would pin old high-priority rows to the top. Scheduling priority belongs to
    # claim_next_task, which has its own ordering.
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        if status:
            async with db.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
    return [_row_to_task(r) for r in rows]


async def claim_next_task(worker_name: str, capabilities: list[str]) -> Task | None:
    """Atomically claim the highest-priority pending task the worker can handle.

    Placement rules (#5b), in order:
      - Hard pin: if _target_machine is set, only that machine may claim it.
      - Soft preference: if _preferred_machine is set, the preferred worker claims
        immediately; any other capable worker may claim only after the task has
        been pending >= OVERFLOW_GRACE_SECS (overflow when the primary is busy/down).
      - No preference: open to any capable worker immediately (unchanged behavior).
    """
    if not capabilities:
        return None
    cap_placeholders = ",".join("?" * len(capabilities))
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        async with db.execute(
            f"""SELECT * FROM tasks
                WHERE status='pending'
                  AND type IN ({cap_placeholders})
                  AND (
                    -- hard pin to this worker
                    json_extract(payload, '$._target_machine') = ?
                    OR (
                      -- not hard-pinned to another machine
                      (json_extract(payload, '$._target_machine') IS NULL
                       OR json_extract(payload, '$._target_machine') = '')
                      AND (
                        -- no soft preference -> any capable worker, immediately
                        json_extract(payload, '$._preferred_machine') IS NULL
                        OR json_extract(payload, '$._preferred_machine') = ''
                        -- preferred to this worker -> immediately
                        OR json_extract(payload, '$._preferred_machine') = ?
                        -- preferred elsewhere -> overflow only after the grace window
                        OR (julianday('now') - julianday(created_at)) * 86400.0 >= ?
                      )
                    )
                  )
                ORDER BY priority DESC, created_at ASC
                LIMIT 1""",
            (*capabilities, worker_name, worker_name, OVERFLOW_GRACE_SECS),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        now = datetime.utcnow().isoformat()
        cur = await db.execute(
            "UPDATE tasks SET status='claimed', assigned_to=?, updated_at=? WHERE id=? AND status='pending'",
            (worker_name, now, row["id"]),
        )
        await db.commit()
        if cur.rowcount == 0:
            # Another worker claimed it between our SELECT and UPDATE.
            return None
    # Build the claimed task from the selected row plus the update delta —
    # avoids a second SELECT on every worker poll.
    task = _row_to_task(row)
    task.status = TaskStatus.claimed
    task.assigned_to = worker_name
    task.updated_at = datetime.fromisoformat(now)
    return task


async def update_task(task_id: str, fields: dict[str, Any]) -> Task | None:
    if not fields:
        return await get_task(task_id)
    fields = dict(fields)
    now = datetime.utcnow().isoformat()
    fields["updated_at"] = now
    if isinstance(fields.get("result"), dict):
        fields.update(tracking.from_result(fields["result"]))      # who answered, tokens, cost
    status = str(getattr(fields.get("status"), "value", fields.get("status")) or "")
    if status in tracking.TERMINAL:
        fields["finished_at"] = now
    if "result" in fields and fields["result"] is not None:
        fields["result"] = json.dumps(fields["result"])
    sets = [f"{k}=?" for k in fields]
    values = list(fields.values())
    if status == "in_progress":                                    # first start only
        sets.append("started_at=COALESCE(started_at, ?)")
        values.append(now)
    set_clause = ", ".join(sets)
    values = values + [task_id]
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        await db.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", values)
        await db.commit()
    return await get_task(task_id)


async def reap_stale(cutoff_iso: str, note: str) -> list[dict]:
    """Move tasks stuck in claimed/in_progress since before `cutoff_iso` (naive UTC
    ISO, same format as updated_at) to needs_human, keeping their result and adding
    `note`. Returns [{id, type, assigned_to}] for the tasks it moved."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(
            "SELECT id, type, assigned_to FROM tasks "
            "WHERE status IN ('claimed','in_progress') AND updated_at < ?", (cutoff_iso,))
        stuck = [dict(r) for r in await cur.fetchall()]
        for t in stuck:
            await db.execute(
                "UPDATE tasks SET status='needs_human', updated_at=?, "
                "notes = CASE WHEN notes IS NULL OR notes='' THEN ? ELSE notes || ' | ' || ? END "
                "WHERE id=? AND status IN ('claimed','in_progress')",
                (now, note, note, t["id"]))
        await db.commit()
    return stuck


_USAGE_COLS = ("id,type,status,assigned_to,agent,model,tokens_in,tokens_out,tokens_total,cost_usd,"
               "started_at,finished_at,created_at,project,sprint,story")


async def usage_rows(since: str | None = None, until: str | None = None,
                     project: str | None = None, sprint: str | None = None, limit: int = 20000) -> list[dict]:
    """Task rows (tracking columns only) for the Scrum/usage summaries."""
    where, params = [], []
    for col, op, val in (("created_at", ">=", since), ("created_at", "<", until),
                         ("project", "=", project), ("sprint", "=", sprint)):
        if val:
            where.append(f"{col} {op} ?")
            params.append(val)
    sql = f"SELECT {_USAGE_COLS} FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + \
          " ORDER BY created_at DESC LIMIT ?"
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(sql, (*params, limit))
        return [dict(r) for r in await cur.fetchall()]
