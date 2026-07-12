"""rqlite-backed task queue — Phase 2 HA backend (parallel to db.py).

Talks to an rqlite node's HTTP API instead of a local SQLite file, so the queue
lives in a Raft cluster and survives losing any single node. Same function
signatures as `orchestrator.db`, so it's a drop-in selected via QUEUE_BACKEND=rqlite
(wired in a follow-up). See docs/rqlite-failover.md.

rqlite HTTP API used (v10):
  POST /db/execute  — writes (DDL/INSERT/UPDATE without RETURNING)
  POST /db/query    — reads (SELECT), with a read-consistency `level`
  POST /db/request  — a write that returns rows (our atomic UPDATE … RETURNING claim)
Statements are JSON arrays: ["SQL with ?", param1, param2, …]. Responses come back
as {"results":[{...}]}, each result carrying either {"columns","values"} (rows),
{"last_insert_id","rows_affected"} (writes), or {"error": "..."}.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import httpx

from shared.models import Task, TaskStatus

RQLITE_URL = os.getenv("RQLITE_URL", "http://localhost:4001").rstrip("/")
OVERFLOW_GRACE_SECS = float(os.getenv("OVERFLOW_GRACE_SECS", "20"))
_HTTP_TIMEOUT = float(os.getenv("RQLITE_HTTP_TIMEOUT", "10"))

# Read consistency (rqlite): "none" = any node, may be stale (fine for dashboards);
# "weak" = leader read (default, consistent enough for scheduling); "strong" =
# linearizable via the Raft log (slowest). Claims never read stale — they go
# through /db/request (a leader write).
_READ_LEVEL = os.getenv("RQLITE_READ_LEVEL", "weak")

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

_COLUMNS = ["id", "type", "status", "priority", "payload", "created_by",
            "assigned_to", "result", "notes", "created_at", "updated_at"]
_RETURNING = ", ".join(_COLUMNS)


class RqliteError(RuntimeError):
    """An rqlite statement returned an error, or the node was unreachable."""


# ── HTTP plumbing ─────────────────────────────────────────────────────────────
async def _post(path: str, statements: list, params: dict | None = None) -> list[dict]:
    """POST statements to an rqlite endpoint; return the `results` list.
    Raises RqliteError on transport failure or any per-statement error."""
    try:
        async with httpx.AsyncClient(base_url=RQLITE_URL, timeout=_HTTP_TIMEOUT) as c:
            r = await c.post(path, params=params or {}, json=statements)
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPError as e:
        raise RqliteError(f"rqlite {path} unreachable: {e}") from e
    results = body.get("results") or []
    for res in results:
        if isinstance(res, dict) and res.get("error"):
            raise RqliteError(f"rqlite {path} error: {res['error']}")
    return results


async def _execute(statements: list) -> list[dict]:
    # Multiple statements run in one transaction (all-or-nothing).
    return await _post("/db/execute", statements, {"transaction": ""})


async def _query(statements: list, level: str | None = None) -> list[dict]:
    return await _post("/db/query", statements, {"level": level or _READ_LEVEL})


async def _request(statements: list) -> list[dict]:
    # Unified endpoint — a write that returns rows (UPDATE … RETURNING). Routed to
    # the leader through Raft, so the claim is atomic even with many orchestrators.
    return await _post("/db/request", statements)


def _rows(result: dict) -> list[dict]:
    """Turn one rqlite result block into a list of column→value dicts."""
    cols = result.get("columns") or []
    return [dict(zip(cols, vals)) for vals in (result.get("values") or [])]


def _row_to_task(d: dict) -> Task:
    d = dict(d)
    d["payload"] = json.loads(d.get("payload") or "{}")
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    d["created_at"] = datetime.fromisoformat(d["created_at"])
    d["updated_at"] = datetime.fromisoformat(d["updated_at"])
    return Task(**d)


# ── Schema ────────────────────────────────────────────────────────────────────
async def ensure_schema() -> None:
    """Create the tasks table if absent. Call once at orchestrator startup —
    rqlite persists it in the Raft log, so it need not run per operation."""
    await _execute([[CREATE_TABLE]])


# ── Queue operations (mirror orchestrator.db) ─────────────────────────────────
async def insert_task(task: Task) -> Task:
    await _execute([[
        """INSERT INTO tasks (id,type,status,priority,payload,created_by,assigned_to,result,notes,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        task.id, task.type, task.status, task.priority,
        json.dumps(task.payload), task.created_by, task.assigned_to,
        json.dumps(task.result) if task.result else None,
        task.notes, task.created_at.isoformat(), task.updated_at.isoformat(),
    ]])
    return task


async def get_task(task_id: str) -> Task | None:
    results = await _query([["SELECT * FROM tasks WHERE id=?", task_id]])
    rows = _rows(results[0]) if results else []
    return _row_to_task(rows[0]) if rows else None


async def list_tasks(status: str | None = None, limit: int = 100) -> list[Task]:
    # Recency-ordered (newest first), matching db.py — inspection view, not scheduling.
    if status:
        stmt = ["SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?", status, limit]
    else:
        stmt = ["SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", limit]
    results = await _query([stmt], level="none")  # dashboards tolerate slightly stale
    return [_row_to_task(r) for r in _rows(results[0])] if results else []


async def claim_next_task(worker_name: str, capabilities: list[str]) -> Task | None:
    """Atomically claim the highest-priority pending task this worker can handle.

    One statement — an `UPDATE … WHERE id=(SELECT … LIMIT 1) AND status='pending'
    RETURNING *` — so there is no SELECT-then-UPDATE race even with the orchestrator
    API running active-active on several nodes. Placement rules (hard pin / soft
    preference + overflow grace) are identical to db.py.
    """
    if not capabilities:
        return None
    caps = ",".join("?" * len(capabilities))
    sql = f"""
        UPDATE tasks
           SET status='claimed', assigned_to=?, updated_at=?
         WHERE id = (
             SELECT id FROM tasks
              WHERE status='pending'
                AND type IN ({caps})
                AND (
                  json_extract(payload, '$._target_machine') = ?
                  OR (
                    (json_extract(payload, '$._target_machine') IS NULL
                     OR json_extract(payload, '$._target_machine') = '')
                    AND (
                      json_extract(payload, '$._preferred_machine') IS NULL
                      OR json_extract(payload, '$._preferred_machine') = ''
                      OR json_extract(payload, '$._preferred_machine') = ?
                      OR (julianday('now') - julianday(created_at)) * 86400.0 >= ?
                    )
                  )
                )
              ORDER BY priority DESC, created_at ASC
              LIMIT 1
           )
           AND status='pending'
        RETURNING {_RETURNING}
    """
    now = datetime.utcnow().isoformat()
    params = [worker_name, now, *capabilities, worker_name, worker_name, OVERFLOW_GRACE_SECS]
    results = await _request([[sql, *params]])
    rows = _rows(results[0]) if results else []
    return _row_to_task(rows[0]) if rows else None


async def update_task(task_id: str, fields: dict[str, Any]) -> Task | None:
    if not fields:
        return await get_task(task_id)
    fields = dict(fields)
    fields["updated_at"] = datetime.utcnow().isoformat()
    if "result" in fields and fields["result"] is not None:
        fields["result"] = json.dumps(fields["result"])
    set_clause = ", ".join(f"{k}=?" for k in fields)
    stmt = [f"UPDATE tasks SET {set_clause} WHERE id=?", *fields.values(), task_id]
    await _execute([stmt])
    return await get_task(task_id)
