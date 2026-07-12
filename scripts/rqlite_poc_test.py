#!/usr/bin/env python3
"""PoC test for the rqlite queue backend (orchestrator/db_rqlite.py).

Exercises the adapter against a live rqlite node ($RQLITE_URL, default
http://localhost:4001): schema, insert, list/get, placement rules, and — the
point of the whole migration — that concurrent claims are atomic (no task is
ever handed to two workers). Run on a box that can reach the rqlite node:

    RQLITE_URL=http://localhost:4001 python scripts/rqlite_poc_test.py

Exit 0 = all assertions passed.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.models import Task, TaskType, TaskStatus  # noqa: E402
from orchestrator import db_rqlite as db  # noqa: E402

WORKER = "thinkpad-x1"
CAPS = ["agent_run"]
OK, FAIL = "✓", "✗"
_fails = 0


def check(cond: bool, msg: str) -> None:
    global _fails
    print(f"  {OK if cond else FAIL} {msg}")
    if not cond:
        _fails += 1


def _task(tid: str, pri: int, payload: dict) -> Task:
    return Task(id=tid, type=TaskType.agent_run, priority=pri,
                payload=payload, created_by="poc-test")


async def _reset() -> None:
    await db.ensure_schema()
    await db._execute([["DELETE FROM tasks"]])


async def main() -> None:
    print(f"rqlite backend @ {db.RQLITE_URL}  (grace={db.OVERFLOW_GRACE_SECS}s)\n")
    await _reset()

    # Seed: two open, one pinned-to-us, one pinned-elsewhere, one soft-pref-elsewhere.
    seed = [
        _task("t-open-lo",   5, {}),
        _task("t-open-hi",   9, {}),
        _task("t-pin-us",    7, {"_target_machine": WORKER}),
        _task("t-pin-other", 8, {"_target_machine": "mac-mini"}),
        _task("t-pref-other", 6, {"_preferred_machine": "mac-mini"}),
    ]
    for t in seed:
        await db.insert_task(t)

    print("insert / get / list")
    got = await db.get_task("t-open-hi")
    check(got is not None and got.priority == 9, "get_task round-trips a task")
    allt = await db.list_tasks()
    check(len(allt) == 5, f"list_tasks returns all 5 (got {len(allt)})")
    check(all(t.status == TaskStatus.pending for t in allt), "all seeded tasks pending")

    print("\nsequential claim — priority order + placement")
    claimed = []
    while True:
        t = await db.claim_next_task(WORKER, CAPS)
        if not t:
            break
        claimed.append(t.id)
    check(claimed == ["t-open-hi", "t-pin-us", "t-open-lo"],
          f"claimed in priority order, placement respected: {claimed}")
    check("t-pin-other" not in claimed, "task pinned to another machine NOT claimed")
    check("t-pref-other" not in claimed, "soft-pref-elsewhere NOT claimed within grace window")
    check(await db.claim_next_task(WORKER, CAPS) is None, "empty queue → claim returns None")

    print("\nconcurrent claim — atomicity (no double-claim)")
    await _reset()
    n = 12
    for i in range(n):
        await db.insert_task(_task(f"c{i:02d}", 5, {}))
    results = await asyncio.gather(*[db.claim_next_task(WORKER, CAPS) for _ in range(n + 8)])
    ids = [r.id for r in results if r is not None]
    check(len(ids) == n, f"exactly {n} tasks claimed ({len(ids)})")
    check(len(set(ids)) == len(ids), "every claimed id is unique — no task claimed twice")
    remaining = await db.list_tasks(status="pending")
    check(remaining == [], "no pending tasks left after concurrent drain")

    print("\nupdate_task")
    done = await db.update_task("c00", {"status": TaskStatus.done, "result": {"ok": True}})
    check(done is not None and done.status == TaskStatus.done, "update_task sets status=done")
    check(done.result == {"ok": True}, "result JSON round-trips")

    print()
    if _fails:
        print(f"{FAIL} {_fails} check(s) FAILED")
        sys.exit(1)
    print(f"{OK} ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
