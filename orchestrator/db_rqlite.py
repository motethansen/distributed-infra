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
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from shared import desk_store as ds, tracking
from shared.desk_errors import DeskConflict, DeskPaused, DeskRateLimited
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
    """Create the tasks table if absent and add the tracking columns. Call once at
    orchestrator startup — rqlite persists it in the Raft log, so it need not run per
    operation. Idempotent, and safe if two orchestrators start at once."""
    await _execute([[CREATE_TABLE]])
    await _execute([[stmt] for stmt in SCRUM_RESPONSES_DDL + SCRUM_SUGGESTIONS_DDL])
    # Desk columns/tables go through Raft, so one ensure_schema reaches every voter; ALTERs are guarded and duplicate-safe.
    res = await _query([["SELECT name FROM pragma_table_info('scrum_responses')"]], level="strong")
    have_resp = {r["name"] for r in _rows(res[0])} if res else set()
    for col, typ in ds.DESK_COLUMNS.items():
        if col not in have_resp:
            try:
                await _execute([[f"ALTER TABLE scrum_responses ADD COLUMN {col} {typ}"]])
            except RqliteError as e:
                if "duplicate column" not in str(e).lower():
                    raise
    await _execute([[stmt] for stmt in ds.DESK_DDL + ds.DESK_INDEX_DDL])
    res = await _query([["SELECT name FROM pragma_table_info('tasks')"]], level="weak")
    have = {r["name"] for r in _rows(res[0])} if res else set()
    for col, typ in tracking.TRACK_COLUMNS.items():
        if col not in have:
            try:
                await _execute([[f"ALTER TABLE tasks ADD COLUMN {col} {typ}"]])
            except RqliteError as e:
                if "duplicate column" not in str(e).lower():   # the other orchestrator won the race
                    raise
    # Backfill from data the rows already hold (only where still empty; derived, not invented).
    for sql in (
        "UPDATE tasks SET agent=json_extract(result,'$.agent') "
        "WHERE agent IS NULL AND result IS NOT NULL AND json_valid(result) AND json_extract(result,'$.agent') IS NOT NULL",
        "UPDATE tasks SET model=json_extract(result,'$.model') "
        "WHERE model IS NULL AND result IS NOT NULL AND json_valid(result) AND json_extract(result,'$.model') IS NOT NULL",
        "UPDATE tasks SET project=json_extract(payload,'$.project') WHERE project IS NULL AND json_valid(payload) "
        "AND json_extract(payload,'$.project') IS NOT NULL",
        "UPDATE tasks SET sprint=json_extract(payload,'$.sprint') WHERE sprint IS NULL AND json_valid(payload) "
        "AND json_extract(payload,'$.sprint') IS NOT NULL",
        "UPDATE tasks SET story=json_extract(payload,'$.story') WHERE story IS NULL AND json_valid(payload) "
        "AND json_extract(payload,'$.story') IS NOT NULL",
        "UPDATE tasks SET role=json_extract(payload,'$.role') WHERE role IS NULL AND json_valid(payload) "
        "AND json_extract(payload,'$.role') IS NOT NULL",
        "UPDATE tasks SET finished_at=updated_at WHERE finished_at IS NULL AND status IN ('done','failed','needs_human')",
    ):
        await _execute([[sql]])


# ── Queue operations (mirror orchestrator.db) ─────────────────────────────────
async def insert_task(task: Task) -> Task:
    tags = tracking.from_payload(task.payload)
    await _execute([[
        """INSERT INTO tasks (id,type,status,priority,payload,created_by,assigned_to,result,notes,created_at,updated_at,
                              project,sprint,story,role)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        task.id, task.type, task.status, task.priority,
        json.dumps(task.payload), task.created_by, task.assigned_to,
        json.dumps(task.result) if task.result else None,
        task.notes, task.created_at.isoformat(), task.updated_at.isoformat(),
        tags.get("project"), tags.get("sprint"), tags.get("story"), tags.get("role"),
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
    now = datetime.utcnow().isoformat()
    fields["updated_at"] = now
    if isinstance(fields.get("result"), dict):
        prior = await get_task(task_id)                            # the type decides the llm/agent label
        fields.update(tracking.for_task(prior.type if prior else None, fields["result"]))  # who answered, tokens, cost
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
    stmt = [f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", *values, task_id]
    await _execute([stmt])
    return await get_task(task_id)


async def reap_stale(cutoff_iso: str, note: str) -> list[dict]:
    """Move tasks stuck in claimed/in_progress since before `cutoff_iso` (naive UTC
    ISO, same format as updated_at) to needs_human, keeping their result and adding
    `note`. One conditional UPDATE, so two orchestrators reaping at once is harmless.
    Returns [{id, type, assigned_to}] for the tasks it moved."""
    now = datetime.utcnow().isoformat()
    results = await _request([[
        """UPDATE tasks
              SET status='needs_human', updated_at=?,
                  notes = CASE WHEN notes IS NULL OR notes='' THEN ? ELSE notes || ' | ' || ? END
            WHERE status IN ('claimed','in_progress') AND updated_at < ?
        RETURNING id, type, assigned_to""",
        now, note, note, cutoff_iso,
    ]])
    return _rows(results[0]) if results else []


_USAGE_COLS = ("id,type,status,assigned_to,agent,model,tokens_in,tokens_out,tokens_total,cost_usd,"
               "tokens_cache_read,tokens_cache_write,llm,"
               "started_at,finished_at,created_at,project,sprint,story,role")


async def usage_rows(since: str | None = None, until: str | None = None,
                     project: str | None = None, sprint: str | None = None, limit: int = 20000) -> list[dict]:
    """Task rows (tracking columns only) for the Scrum/usage summaries. Dashboard read: may be
    slightly stale."""
    where, params = [], []
    for col, op, val in (("created_at", ">=", since), ("created_at", "<", until),
                         ("project", "=", project), ("sprint", "=", sprint)):
        if val:
            where.append(f"{col} {op} ?")
            params.append(val)
    sql = f"SELECT {_USAGE_COLS} FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + \
          " ORDER BY created_at DESC LIMIT ?"
    res = await _query([[sql, *params, limit]], level="none")
    return _rows(res[0]) if res else []


# ── Scrum responses (owner answers to board items; append-only) ──────────────

async def insert_scrum_response(fields: dict, idempotency_key: str | None = None) -> dict:
    """Append a response. A repeat of the same idempotency_key within 10s returns the existing row."""
    if idempotency_key is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_IDEM_WINDOW_SECS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        res = await _query([["SELECT id, created_at, status, item_key, project, item_id, section, action, suggestion_id, suggestion_text, comment, actor FROM scrum_responses WHERE idempotency_key = ? "
                             "AND created_at >= ? ORDER BY id ASC LIMIT 1", idempotency_key, cutoff]])
        rows = _rows(res[0]) if res else []
        if rows:
            return _replay_or_conflict(rows[0], fields)
    created_at = _utc_now_iso()
    res = await _execute([[_INSERT_RESP, *_resp_params(fields, created_at, idempotency_key)],
                          [ds.AUDIT_CREATE, "recorded", fields["actor"], created_at]])
    return {"id": res[0]["last_insert_id"], "created_at": created_at, "status": "recorded"}


async def list_scrum_responses(status: str | None = None, project: str | None = None,
                               latest: bool = False) -> list[dict]:
    """Newest first. latest=True keeps only the newest row per item_key."""
    sql, params = _list_resp_sql(status, project, latest)
    res = await _query([[sql, *params]], level="strong")   # an owner's answer must never read stale
    return _rows(res[0]) if res else []


async def _one(sql: str, *params, level: str = "strong") -> dict | None:
    res = await _query([[sql, *params]], level=level)
    rows = _rows(res[0]) if res else []
    return rows[0] if rows else None


async def consume_scrum_response(response_id: int, consumed_by: str) -> str:
    """'ok' | 'not_found' | 'already_consumed'. One conditional UPDATE, so only one caller wins.
    A command row is claimed queued->running instead, and raises DeskPaused while the desk is paused."""
    found = await _one(ds.KIND_STATUS, response_id)
    if found is None:
        return "not_found"
    now = _utc_now_iso()
    if found["kind"] == "command":
        pause = await _one(ds.GET_PAUSE)
        if pause and pause["paused"]:
            raise DeskPaused("desk paused")
        res = await _execute([[ds.CLAIM_COMMAND, now, consumed_by, response_id],
                              [ds.AUDIT_IF_CHANGED, response_id, "queued", "running", consumed_by, now]])
    else:
        res = await _execute([[
            "UPDATE scrum_responses SET status='consumed', consumed_at=?, consumed_by=? WHERE id=? AND status='recorded'",
            now, consumed_by, response_id],
            [ds.AUDIT_IF_CHANGED, response_id, "recorded", "consumed", consumed_by, now]])
    if res and res[0].get("rows_affected", 0) > 0:
        return "ok"
    if found["kind"] == "command" and found["status"] == "queued":
        pause = await _one(ds.GET_PAUSE)   # a pause that landed between the check and the claim
        if pause and pause["paused"]:
            raise DeskPaused("desk paused")
    return "already_consumed"


# ── Desk commands (kind='command' rows in scrum_responses) ───────────────────

async def insert_command(fields: dict, idempotency_key: str | None = None, rate_limit: int | None = None) -> dict:
    """Queue a command. Same Idempotency-Key within 10s replays it; with a different body it raises ValueError (409).
    With rate_limit, the count and the insert are one statement; DeskRateLimited (429) when the actor is at the limit."""
    if idempotency_key is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_IDEM_WINDOW_SECS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        row = await _one(ds.COMMAND_IDEM_SELECT, idempotency_key, cutoff)
        if row:
            return ds.command_replay_or_conflict(row, fields)
    created_at = _utc_now_iso()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ds.COMMAND_RATE_WINDOW_SECS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    res = await _execute([[ds.INSERT_COMMAND_LIMITED,
                           *ds.limited_command_params(fields, created_at, idempotency_key, cutoff, rate_limit)],
                          [ds.AUDIT_CREATE_IF_INSERTED, "queued", fields["actor"], created_at]])
    if not res or res[0].get("rows_affected", 0) < 1:
        if idempotency_key is not None:   # lost the race on the unique key: replay the winner, even at the limit
            row = await _one(ds.COMMAND_IDEM_LOOKUP, idempotency_key)
            if row:
                return ds.command_replay_or_conflict(row, fields)
        raise DeskRateLimited("rate limited")
    return {"id": res[0]["last_insert_id"], "created_at": created_at, "status": "queued"}


async def transition_result(response_id: int, status: str, summary: str, actor: str = "unknown") -> str:
    """running -> done|failed, once. 'ok' | 'not_found'; DeskConflict (409) from any other state; ValueError on bad input."""
    summary = ds.check_result(status, summary)
    res = await _execute([[ds.RESULT_COMMAND, status, status, summary, response_id],
                          [ds.AUDIT_IF_CHANGED, response_id, "running", status, actor, _utc_now_iso()]])
    if res and res[0].get("rows_affected", 0) > 0:
        return "ok"
    found = await _one(ds.KIND_STATUS, response_id)
    if found is None:
        return "not_found"
    raise DeskConflict(f"cannot move {found['kind']} from {found['status']} to {status}")


async def get_pause() -> dict:
    return ds.pause_row(await _one(ds.GET_PAUSE))


async def set_pause(paused: bool, by: str, reason: str | None = None) -> dict:
    """Pause or resume the desk. A running command is left alone; only the next claim is refused."""
    now = _utc_now_iso()
    await _execute([[ds.AUDIT_PAUSE, "paused" if paused else "active", by, now],
                    [ds.SET_PAUSE, 1 if paused else 0, by, now, (reason or "")[:500] or None]])
    return await get_pause()


async def count_commands_last_hour(actor: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ds.COMMAND_RATE_WINDOW_SECS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row = await _one(ds.COUNT_COMMANDS, actor, cutoff)
    return int(row["n"]) if row else 0


async def list_commands(kind: str = "command", limit: int = 200) -> list[dict]:
    res = await _query([[ds.LIST_COMMANDS, kind, limit]], level="strong")
    return _rows(res[0]) if res else []


async def list_worker_commands(status: str, limit: int = 50) -> list[dict]:
    res = await _query([[ds.LIST_WORKER_COMMANDS, status, limit]], level="strong")
    return _rows(res[0]) if res else []


# ── Scrum suggestions (one Sonnet-written next step per item and evidence; old rows stay readable) ──

async def get_scrum_suggestion(item_key: str, evidence_hash: str) -> dict | None:
    res = await _query([[f"SELECT {_SUG_COLS} FROM scrum_suggestions WHERE item_key = ? AND evidence_hash = ?",
                         item_key, evidence_hash]], level="strong")
    rows = _rows(res[0]) if res else []
    return rows[0] if rows else None


_SUG_BATCH = 400          # 2 variables per pair; stays under SQLite's 999-variable limit


async def get_scrum_suggestions(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """Batch of get_scrum_suggestion: {(item_key, evidence_hash): row} for the pairs that have a stored row."""
    pairs = list(dict.fromkeys(pairs))
    out: dict[tuple[str, str], dict] = {}
    chunks = [pairs[i:i + _SUG_BATCH] for i in range(0, len(pairs), _SUG_BATCH)]
    if not chunks:
        return out
    stmts = [[f"SELECT {_SUG_COLS}, item_key, evidence_hash FROM scrum_suggestions WHERE "
              + " OR ".join("(item_key = ? AND evidence_hash = ?)" for _ in c), *[v for pair in c for v in pair]] for c in chunks]
    for res in await _query(stmts, level="strong"):          # one round trip for all chunks
        for d in _rows(res):
            out[(d.pop("item_key"), d.pop("evidence_hash"))] = d
    return out


async def get_scrum_suggestion_by_id(suggestion_id: str) -> dict | None:
    res = await _query([[f"SELECT {_SUG_COLS}, item_key, evidence_hash FROM scrum_suggestions WHERE id = ?", suggestion_id]],
                       level="strong")
    rows = _rows(res[0]) if res else []
    return rows[0] if rows else None


async def save_scrum_suggestion(row: dict) -> None:
    """Keep the first suggestion stored for (item_key, evidence_hash); a concurrent duplicate is ignored."""
    await _execute([["INSERT OR IGNORE INTO scrum_suggestions (id, item_key, evidence_hash, text, model, generated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     row["id"], row["item_key"], row["evidence_hash"], row["text"], row["model"], row["generated_at"]]])
