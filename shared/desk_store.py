"""SQL and helpers the desk command rows share between orchestrator.db (SQLite) and orchestrator.db_rqlite.

Commands live in the existing scrum_responses table (kind='command'); nothing here rebuilds it. New columns are added
with an idempotent ALTER (see DESK_COLUMNS), the pause switch and the audit trail get their own tables.
Request bodies are never logged here or by the callers.
"""
from __future__ import annotations

import json

RESULT_SUMMARY_MAX = 2000
RESULT_STATUSES = ("done", "failed")
COMMAND_RATE_WINDOW_SECS = 3600

# Added to scrum_responses by an idempotent ALTER TABLE ADD COLUMN; old rows read as kind='response' via COALESCE.
DESK_COLUMNS = {
    "kind": "TEXT DEFAULT 'response'",
    "verb": "TEXT",
    "sprint": "TEXT",
    "story_id": "TEXT",
    "plan_digest": "TEXT",
    "evidence": "TEXT",
    "tier": "TEXT",
    "result_status": "TEXT",
    "result_summary": "TEXT",
}

DESK_DDL = [
    """
    CREATE TABLE IF NOT EXISTS desk_pause (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        paused INTEGER NOT NULL DEFAULT 0,
        set_by TEXT NULL,
        set_at TEXT NULL,
        reason TEXT NULL
    )
    """,
    "INSERT OR IGNORE INTO desk_pause (id, paused) VALUES (1, 0)",
    """
    CREATE TABLE IF NOT EXISTS scrum_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        response_id INTEGER NULL,
        from_status TEXT NULL,
        to_status TEXT NOT NULL,
        actor TEXT NOT NULL,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scrum_audit_response_id ON scrum_audit (response_id, id)",
    # Append-only: the database itself refuses edits and deletes.
    """
    CREATE TRIGGER IF NOT EXISTS scrum_audit_no_update BEFORE UPDATE ON scrum_audit
    BEGIN SELECT RAISE(ABORT, 'scrum_audit is append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS scrum_audit_no_delete BEFORE DELETE ON scrum_audit
    BEGIN SELECT RAISE(ABORT, 'scrum_audit is append-only'); END
    """,
]

# One command per Idempotency-Key, enforced by the database so simultaneous retries cannot both insert. Additive and
# partial (command rows only), so it applies to an existing table; it needs the `kind` column, so it runs after the ALTERs.
DESK_INDEX_DDL = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_scrum_responses_command_idem ON scrum_responses (idempotency_key) "
    "WHERE kind = 'command' AND idempotency_key IS NOT NULL",
]

KIND = "COALESCE(kind, 'response')"
COMMAND_PUBLIC = "id, status, tier, actor, created_at, verb, result_summary"

INSERT_COMMAND = ("INSERT INTO scrum_responses (item_key, project, section, action, actor, created_at, status, "
                  "idempotency_key, kind, verb, sprint, story_id, plan_digest, evidence, tier) "
                  "VALUES (?, ?, 'desk', 'command', ?, ?, 'queued', ?, 'command', ?, ?, ?, ?, ?, ?)")
# Count-and-insert as ONE statement: the row is added only while the actor has fewer than `limit` commands since
# `cutoff`, so concurrent requests cannot both pass a check that only one of them should. Params: the INSERT_COMMAND
# values, then actor, cutoff, limit.
INSERT_COMMAND_LIMITED = ("INSERT INTO scrum_responses (item_key, project, section, action, actor, created_at, status, "
                          "idempotency_key, kind, verb, sprint, story_id, plan_digest, evidence, tier) "
                          "SELECT ?, ?, 'desk', 'command', ?, ?, 'queued', ?, 'command', ?, ?, ?, ?, ?, ? "
                          "WHERE (SELECT COUNT(*) FROM scrum_responses WHERE COALESCE(kind, 'response')='command' "
                          "AND actor=? AND created_at>=?) < ? "
                          "ON CONFLICT DO NOTHING")
NO_RATE_LIMIT = 1 << 62
COMMAND_IDEM_FIELDS = ("project", "actor", "verb", "sprint", "story_id", "plan_digest", "evidence", "tier")
COMMAND_IDEM_SELECT = ("SELECT id, created_at, status, kind, " + ", ".join(COMMAND_IDEM_FIELDS) +
                       " FROM scrum_responses WHERE idempotency_key = ? AND created_at >= ? ORDER BY id ASC LIMIT 1")
# After an insert that added nothing: the row that already holds the key (outside the replay window too, since the
# unique index still holds it). No row means the insert was refused by the rate limit instead.
COMMAND_IDEM_LOOKUP = ("SELECT id, created_at, status, kind, " + ", ".join(COMMAND_IDEM_FIELDS) +
                       " FROM scrum_responses WHERE idempotency_key = ? AND kind = 'command' ORDER BY id ASC LIMIT 1")

# Each audit row is written in the same transaction as the change it records.
AUDIT_CREATE = ("INSERT INTO scrum_audit (response_id, from_status, to_status, actor, at) "
                "VALUES (last_insert_rowid(), NULL, ?, ?, ?)")
# Only when the preceding INSERT actually added a row (a rate-limited INSERT ... SELECT adds none).
AUDIT_CREATE_IF_INSERTED = ("INSERT INTO scrum_audit (response_id, from_status, to_status, actor, at) "
                            "SELECT last_insert_rowid(), NULL, ?, ?, ? WHERE changes() > 0")
AUDIT_IF_CHANGED = ("INSERT INTO scrum_audit (response_id, from_status, to_status, actor, at) "
                    "SELECT ?, ?, ?, ?, ? WHERE changes() > 0")
AUDIT_PAUSE = ("INSERT INTO scrum_audit (response_id, from_status, to_status, actor, at) "
               "SELECT NULL, CASE paused WHEN 1 THEN 'paused' ELSE 'active' END, ?, ?, ? FROM desk_pause WHERE id = 1")

# queued -> running only if the desk is not paused (the guard keeps a racing pause from being overtaken).
CLAIM_COMMAND = ("UPDATE scrum_responses SET status='running', consumed_at=?, consumed_by=? "
                 "WHERE id=? AND " + KIND + "='command' AND status='queued' "
                 "AND NOT EXISTS (SELECT 1 FROM desk_pause WHERE id=1 AND paused=1)")
RESULT_COMMAND = ("UPDATE scrum_responses SET status=?, result_status=?, result_summary=? "
                  "WHERE id=? AND " + KIND + "='command' AND status='running'")
SET_PAUSE = "UPDATE desk_pause SET paused=?, set_by=?, set_at=?, reason=? WHERE id=1"
GET_PAUSE = "SELECT paused, set_by, set_at, reason FROM desk_pause WHERE id=1"
COUNT_COMMANDS = "SELECT COUNT(*) AS n FROM scrum_responses WHERE " + KIND + "='command' AND actor=? AND created_at>=?"
LIST_COMMANDS = ("SELECT " + COMMAND_PUBLIC + " FROM scrum_responses WHERE " + KIND + "=? ORDER BY id DESC LIMIT ?")
KIND_STATUS = "SELECT " + KIND + " AS kind, status FROM scrum_responses WHERE id=?"


def command_params(fields: dict, created_at: str, idempotency_key: str | None) -> tuple:
    verb = fields["verb"]
    ev = fields.get("evidence")
    if ev is not None and not isinstance(ev, str):
        ev = json.dumps(ev, sort_keys=True)
    return (f"command:{verb}", fields["project"], fields["actor"], created_at, idempotency_key, verb,
            fields.get("sprint"), fields.get("story_id"), fields.get("plan_digest"), ev, fields.get("tier"))


def limited_command_params(fields: dict, created_at: str, idempotency_key: str | None, cutoff: str,
                           limit: int | None) -> tuple:
    return (*command_params(fields, created_at, idempotency_key), fields["actor"], cutoff,
            NO_RATE_LIMIT if limit is None else limit)


def command_replay_or_conflict(row: dict, fields: dict) -> dict:
    """Same Idempotency-Key + same command replays the original answer; a different body is a conflict (409)."""
    want = dict(fields)
    ev = want.get("evidence")
    if ev is not None and not isinstance(ev, str):
        want["evidence"] = json.dumps(ev, sort_keys=True)
    if (row.get("kind") or "response") != "command" or any(
            (row.get(k) or None) != (want.get(k) or None) for k in COMMAND_IDEM_FIELDS):
        raise ValueError("idempotency_conflict")
    return {"id": row["id"], "created_at": row["created_at"], "status": row["status"], "replayed": True}


def check_result(status: str, summary: str) -> str:
    if status not in RESULT_STATUSES:
        raise ValueError("status must be done or failed")
    summary = summary or ""
    if len(summary) > RESULT_SUMMARY_MAX:
        raise ValueError(f"summary is longer than {RESULT_SUMMARY_MAX} characters")
    return summary


def pause_row(row: dict | None) -> dict:
    row = row or {}
    return {"paused": bool(row.get("paused")), "by": row.get("set_by"), "at": row.get("set_at"),
            "reason": row.get("reason")}
