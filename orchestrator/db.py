"""SQLite-backed task queue. Async-safe via aiosqlite."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from shared import desk_store as ds, tracking
from shared.desk_errors import DeskConflict, DeskPaused, DeskRateLimited
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

SCRUM_RESPONSES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS scrum_responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_key TEXT NOT NULL,
        project TEXT NOT NULL,
        item_id TEXT NULL,
        section TEXT NOT NULL,
        action TEXT NOT NULL,
        suggestion_id TEXT NULL,
        suggestion_text TEXT NULL,
        comment TEXT NULL,
        actor TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'recorded',
        consumed_at TEXT NULL,
        consumed_by TEXT NULL,
        idempotency_key TEXT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scrum_responses_item_key_id ON scrum_responses (item_key, id)",
    "CREATE INDEX IF NOT EXISTS idx_scrum_responses_status_id ON scrum_responses (status, id)",
    "CREATE INDEX IF NOT EXISTS idx_scrum_responses_idempotency_key ON scrum_responses (idempotency_key)",
]
SCRUM_SUGGESTIONS_DDL = [
    """
    CREATE TABLE IF NOT EXISTS scrum_suggestions (
        id TEXT PRIMARY KEY,
        item_key TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        text TEXT NOT NULL,
        model TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        UNIQUE (item_key, evidence_hash)
    )
    """,
]
_SUG_COLS = "id, text, generated_at, model"
_RESP_PUBLIC = ("id, item_key, project, item_id, section, action, suggestion_id, suggestion_text, comment, "
                "actor, created_at, status, consumed_at, consumed_by, COALESCE(kind, 'response') AS kind")
_INSERT_RESP = ("INSERT INTO scrum_responses (item_key, project, item_id, section, action, suggestion_id, "
                "suggestion_text, comment, actor, created_at, status, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded', ?)")
_IDEM_WINDOW_SECS = 10


_IDEM_FIELDS = ("item_key", "project", "item_id", "section", "action", "suggestion_id", "suggestion_text", "comment", "actor")


def _replay_or_conflict(row: dict, fields: dict) -> dict:
    """Same idempotency key seen again: an identical request replays the original answer; a different one is a conflict
    (never silently drop a distinct response)."""
    if any((row.get(k) or None) != (fields.get(k) or None) for k in _IDEM_FIELDS):
        raise ValueError("idempotency_conflict")
    return {"id": row["id"], "created_at": row["created_at"], "status": "recorded", "replayed": True}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _resp_params(fields: dict, created_at: str, idempotency_key: str | None) -> tuple:
    return (fields["item_key"], fields["project"], fields.get("item_id"), fields["section"], fields["action"],
            fields.get("suggestion_id"), fields.get("suggestion_text"), fields.get("comment"), fields["actor"],
            created_at, idempotency_key)


def _list_resp_sql(status: str | None, project: str | None, latest: bool) -> tuple[str, list]:
    where, params = [], []
    for col, val in (("status", status), ("project", project)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    if latest:   # newest row per item_key among the rows that match the filters
        return (f"SELECT {_RESP_PUBLIC} FROM scrum_responses WHERE id IN "
                f"(SELECT MAX(id) FROM scrum_responses {w} GROUP BY item_key) ORDER BY id DESC"), params
    return f"SELECT {_RESP_PUBLIC} FROM scrum_responses {w} ORDER BY id DESC", params


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
    for stmt in SCRUM_RESPONSES_DDL + SCRUM_SUGGESTIONS_DDL + ds.DESK_DDL:
        await db.execute(stmt)
    path = _db_path()
    if path not in _MIGRATED:
        cur = await db.execute("PRAGMA table_info(scrum_responses)")
        have_resp = {r[1] for r in await cur.fetchall()}
        for col, typ in ds.DESK_COLUMNS.items():
            if col not in have_resp:
                try:
                    await db.execute(f"ALTER TABLE scrum_responses ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise
        cur = await db.execute("PRAGMA table_info(tasks)")
        have = {r[1] for r in await cur.fetchall()}
        for col, typ in tracking.TRACK_COLUMNS.items():
            if col not in have:
                try:
                    await db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise                    # otherwise a concurrent first request added it between our check and now
        # Backfill from data the rows already hold (only where still empty).
        for col in ("agent", "model"):
            await db.execute(f"UPDATE tasks SET {col}=json_extract(result,'$.{col}') WHERE {col} IS NULL "
                             f"AND result IS NOT NULL AND json_valid(result) AND json_extract(result,'$.{col}') IS NOT NULL")
        for col in ("project", "sprint", "story", "role"):
            await db.execute(f"UPDATE tasks SET {col}=json_extract(payload,'$.{col}') WHERE {col} IS NULL "
                             f"AND json_valid(payload) AND json_extract(payload,'$.{col}') IS NOT NULL")
        await db.execute("UPDATE tasks SET finished_at=updated_at WHERE finished_at IS NULL "
                         "AND status IN ('done','failed','needs_human')")
        for stmt in ds.DESK_INDEX_DDL:
            await db.execute(stmt)
        _MIGRATED.add(path)
    await db.commit()


async def insert_task(task: Task) -> Task:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        tags = tracking.from_payload(task.payload)
        await db.execute(
            """INSERT INTO tasks (id,type,status,priority,payload,created_by,assigned_to,result,notes,created_at,updated_at,
                                  project,sprint,story,role)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task.id, task.type, task.status, task.priority,
                json.dumps(task.payload), task.created_by, task.assigned_to,
                json.dumps(task.result) if task.result else None,
                task.notes,
                task.created_at.isoformat(), task.updated_at.isoformat(),
                tags.get("project"), tags.get("sprint"), tags.get("story"), tags.get("role"),
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
               "tokens_cache_read,tokens_cache_write,llm,"
               "started_at,finished_at,created_at,project,sprint,story,role")


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


# ── Scrum responses (owner answers to board items; append-only) ──────────────

async def insert_scrum_response(fields: dict, idempotency_key: str | None = None) -> dict:
    """Append a response. A repeat of the same idempotency_key within 10s returns the existing row."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        if idempotency_key is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_IDEM_WINDOW_SECS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            cur = await db.execute(
                "SELECT id, created_at, status, item_key, project, item_id, section, action, suggestion_id, suggestion_text, comment, actor FROM scrum_responses WHERE idempotency_key = ? AND created_at >= ? "
                "ORDER BY id ASC LIMIT 1", (idempotency_key, cutoff))
            row = await cur.fetchone()
            if row:
                return _replay_or_conflict(dict(row), fields)
        created_at = _utc_now_iso()
        cur = await db.execute(_INSERT_RESP, _resp_params(fields, created_at, idempotency_key))
        await db.execute(ds.AUDIT_CREATE, ("recorded", fields["actor"], created_at))
        await db.commit()
        return {"id": cur.lastrowid, "created_at": created_at, "status": "recorded"}


async def list_scrum_responses(status: str | None = None, project: str | None = None,
                               latest: bool = False) -> list[dict]:
    """Newest first. latest=True keeps only the newest row per item_key."""
    sql, params = _list_resp_sql(status, project, latest)
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def consume_scrum_response(response_id: int, consumed_by: str) -> str:
    """'ok' | 'not_found' | 'already_consumed'. One conditional UPDATE, so only one caller wins.
    A command row is claimed queued->running instead, and raises DeskPaused while the desk is paused."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(ds.KIND_STATUS, (response_id,))
        found = await cur.fetchone()
        if found is None:
            return "not_found"
        now = _utc_now_iso()
        if found["kind"] == "command":
            cur = await db.execute(ds.GET_PAUSE)
            pause = await cur.fetchone()
            if pause and pause["paused"]:
                raise DeskPaused("desk paused")
            cur = await db.execute(ds.CLAIM_COMMAND, (now, consumed_by, response_id))
            await db.execute(ds.AUDIT_IF_CHANGED, (response_id, "queued", "running", consumed_by, now))
        else:
            cur = await db.execute(
                "UPDATE scrum_responses SET status='consumed', consumed_at=?, consumed_by=? WHERE id=? AND status='recorded'",
                (now, consumed_by, response_id))
            await db.execute(ds.AUDIT_IF_CHANGED, (response_id, "recorded", "consumed", consumed_by, now))
        await db.commit()
        if cur.rowcount > 0:
            return "ok"
        cur = await db.execute(ds.GET_PAUSE)   # a pause that landed between the check and the claim
        pause = await cur.fetchone()
        if found["kind"] == "command" and found["status"] == "queued" and pause and pause["paused"]:
            raise DeskPaused("desk paused")
        return "already_consumed"


# ── Desk commands (kind='command' rows in scrum_responses) ───────────────────

async def insert_command(fields: dict, idempotency_key: str | None = None, rate_limit: int | None = None) -> dict:
    """Queue a command. Same Idempotency-Key within 10s replays it; with a different body it raises ValueError (409).
    With rate_limit, the count and the insert are one statement; DeskRateLimited (429) when the actor is at the limit."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        if idempotency_key is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_IDEM_WINDOW_SECS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            cur = await db.execute(ds.COMMAND_IDEM_SELECT, (idempotency_key, cutoff))
            row = await cur.fetchone()
            if row:
                return ds.command_replay_or_conflict(dict(row), fields)
        created_at = _utc_now_iso()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ds.COMMAND_RATE_WINDOW_SECS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cur = await db.execute(ds.INSERT_COMMAND_LIMITED,
                               ds.limited_command_params(fields, created_at, idempotency_key, cutoff, rate_limit))
        inserted, row_id = cur.rowcount, cur.lastrowid
        await db.execute(ds.AUDIT_CREATE_IF_INSERTED, ("queued", fields["actor"], created_at))
        await db.commit()
        if inserted < 1:
            if idempotency_key is not None:   # lost the race on the unique key: replay the winner, even at the limit
                cur = await db.execute(ds.COMMAND_IDEM_LOOKUP, (idempotency_key,))
                row = await cur.fetchone()
                if row:
                    return ds.command_replay_or_conflict(dict(row), fields)
            raise DeskRateLimited("rate limited")
        return {"id": row_id, "created_at": created_at, "status": "queued"}


async def transition_result(response_id: int, status: str, summary: str, actor: str = "unknown") -> str:
    """running -> done|failed, once. 'ok' | 'not_found'; DeskConflict (409) from any other state; ValueError on bad input."""
    summary = ds.check_result(status, summary)
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        now = _utc_now_iso()
        cur = await db.execute(ds.RESULT_COMMAND, (status, status, summary, response_id))
        await db.execute(ds.AUDIT_IF_CHANGED, (response_id, "running", status, actor, now))
        await db.commit()
        if cur.rowcount > 0:
            return "ok"
        cur = await db.execute(ds.KIND_STATUS, (response_id,))
        found = await cur.fetchone()
        if found is None:
            return "not_found"
        raise DeskConflict(f"cannot move {found['kind']} from {found['status']} to {status}")


async def get_pause() -> dict:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(ds.GET_PAUSE)
        row = await cur.fetchone()
        return ds.pause_row(dict(row) if row else None)


async def set_pause(paused: bool, by: str, reason: str | None = None) -> dict:
    """Pause or resume the desk. A running command is left alone; only the next claim is refused."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        now = _utc_now_iso()
        await db.execute(ds.AUDIT_PAUSE, ("paused" if paused else "active", by, now))
        await db.execute(ds.SET_PAUSE, (1 if paused else 0, by, now, (reason or "")[:500] or None))
        await db.commit()
    return await get_pause()


async def count_commands_last_hour(actor: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ds.COMMAND_RATE_WINDOW_SECS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(ds.COUNT_COMMANDS, (actor, cutoff))
        return (await cur.fetchone())["n"]


async def list_commands(kind: str = "command", limit: int = 200) -> list[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(ds.LIST_COMMANDS, (kind, limit))
        return [dict(r) for r in await cur.fetchall()]


# ── Scrum suggestions (one Sonnet-written next step per item and evidence; old rows stay readable) ──

async def get_scrum_suggestion(item_key: str, evidence_hash: str) -> dict | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(f"SELECT {_SUG_COLS} FROM scrum_suggestions WHERE item_key = ? AND evidence_hash = ?",
                               (item_key, evidence_hash))
        row = await cur.fetchone()
        return dict(row) if row else None


_SUG_BATCH = 400          # 2 variables per pair; stays under SQLite's 999-variable limit


async def get_scrum_suggestions(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """Batch of get_scrum_suggestion: {(item_key, evidence_hash): row} for the pairs that have a stored row."""
    pairs = list(dict.fromkeys(pairs))
    out: dict[tuple[str, str], dict] = {}
    if not pairs:
        return out
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        for i in range(0, len(pairs), _SUG_BATCH):
            chunk = pairs[i:i + _SUG_BATCH]
            where = " OR ".join("(item_key = ? AND evidence_hash = ?)" for _ in chunk)
            cur = await db.execute(f"SELECT {_SUG_COLS}, item_key, evidence_hash FROM scrum_suggestions WHERE {where}",
                                   [v for pair in chunk for v in pair])
            for r in await cur.fetchall():
                d = dict(r)
                out[(d.pop("item_key"), d.pop("evidence_hash"))] = d
    return out


async def get_scrum_suggestion_by_id(suggestion_id: str) -> dict | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(f"SELECT {_SUG_COLS}, item_key, evidence_hash FROM scrum_suggestions WHERE id = ?", (suggestion_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def save_scrum_suggestion(row: dict) -> None:
    """Keep the first suggestion stored for (item_key, evidence_hash); a concurrent duplicate is ignored."""
    async with aiosqlite.connect(_db_path()) as db:
        await _ensure_schema(db)
        await db.execute("INSERT OR IGNORE INTO scrum_suggestions (id, item_key, evidence_hash, text, model, generated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (row["id"], row["item_key"], row["evidence_hash"], row["text"], row["model"], row["generated_at"]))
        await db.commit()
