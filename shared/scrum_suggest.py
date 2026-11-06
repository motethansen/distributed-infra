"""Scrum Master suggestions for the board: one concrete next step per review item, decision and adjustment.

A suggestion is keyed by (item_key, evidence_hash): unchanged evidence reuses the stored text (no model call);
changed evidence stores a new row and leaves the old one readable, so an earlier response still resolves its
suggestion_id. The model call is injected, so the orchestrator owns how Sonnet is reached and tests count calls.
A failed or slow call leaves `suggestion: None`; it never breaks the board.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from shared.scrum_digest import evidence_text

log = logging.getLogger(__name__)

MAX_CHARS = 300
MAX_PARALLEL = 3              # model calls in flight at once
LOOKUP_DEADLINE_SECS = 4.0    # hard bound on all cache lookups together: slow storage never makes a slow board
SLOW_LOOKUP_LOG_SECS = 1.0    # a lookup slower than this (finished or not) logs one line
KNOWN_MAX = 2000              # last-known-good rows kept in this process
CALL_TIMEOUT_SECS = 12.0
MODEL = "sonnet"

PROMPT = """You are the Scrum Master. Suggest ONE concrete next step for the product owner on the board item below.

Rules:
- One sentence, at most 250 characters, plain text, no markdown, no list.
- Name exactly one action the owner or the team can do next (who does what), not a general principle.
- Use only the facts given; do not invent story ids, dates or numbers.

Item: {item_key}
Section: {section}
Facts: {evidence}
{hint}
Reply with the sentence only."""


_HUMAN = re.compile(r"human[- ]only|human step|\b(?:console|dashboard)\b", re.I)


def hint(entry: dict) -> str:
    """Extra prompt rules for the two edge cases: a blocked item with no recorded reason, and a human-only step."""
    out = []
    if entry.get("kind") == "blocked" and not (entry.get("reason") or "").strip():
        out.append("- No reason is recorded for this block: the step is to ask the owner of the item for the reason. Do not guess or invent a cause.")
    if _HUMAN.search(evidence_text(entry)):
        out.append("- This needs a human-only step: name that step and the console or dashboard it happens in, and tell the human to do it. "
                   "Do NOT suggest that an agent act on it.")
    return "\n".join(out) + ("\n" if out else "")


def evidence_hash(entry: dict) -> str:
    return hashlib.sha256(evidence_text(entry).encode()).hexdigest()[:16]


def suggestion_id(item_key: str, ev_hash: str) -> str:
    return "sg_" + hashlib.sha256(f"{item_key}|{ev_hash}".encode()).hexdigest()[:12]


def clean(text: str | None) -> str | None:
    """One line, at most MAX_CHARS; None when there is nothing usable."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return None
    one = " ".join(lines[0].split())
    if len(one) > MAX_CHARS:
        cut = one[:MAX_CHARS - 1]
        one = (cut[:cut.rfind(" ")] if " " in cut[MAX_CHARS // 2:] else cut).rstrip(" ,;:—-") + "…"
    return one


def _public(row: dict) -> dict:
    return {k: row[k] for k in ("id", "text", "generated_at", "model")}


_SECTIONS = (("review", "review_queue"), ("decision", "decisions"), ("adjustment", "adjustments"))
_IN_FLIGHT: set[tuple[str, str]] = set()       # (item_key, evidence_hash) being generated, so loads never double-call
_TASKS: set[asyncio.Task] = set()


def _entries(digest: dict[str, Any]) -> list[tuple[str, dict]]:
    return [(sec, e) for sec, key in _SECTIONS for e in digest.get(key) or [] if isinstance(e, dict) and e.get("item_key")]


_KNOWN: "OrderedDict[tuple[str, str], dict]" = OrderedDict()    # last row seen per (item_key, evidence_hash), LRU


def remember(item_key: str, ev_hash: str, row: dict) -> None:
    _KNOWN[(item_key, ev_hash)] = _public(row)
    _KNOWN.move_to_end((item_key, ev_hash))
    while len(_KNOWN) > KNOWN_MAX:
        _KNOWN.popitem(last=False)


def _known(item_key: str, ev_hash: str) -> dict | None:
    row = _KNOWN.get((item_key, ev_hash))                 # same evidence hash only: a changed hash is a new suggestion
    if row is not None:
        _KNOWN.move_to_end((item_key, ev_hash))
    return row


async def attach_cached(digest: dict[str, Any], store: Any, unresolved: set[str] | None = None) -> dict[str, Any]:
    """Set `suggestion` on every review/decision/adjustment entry from the store only: the stored row or None.
    Never calls the model. In place; returned for convenience. `store` has get_scrum_suggestions(pairs) (one query)
    or else get_scrum_suggestion(item_key, hash). All lookups share one hard deadline (LOOKUP_DEADLINE_SECS).
    When the store is slow or fails, an entry gets the last row seen for the SAME evidence hash, so ids stay stable.
    Item keys with no answer at all are added to `unresolved`: their suggestion may exist, so the caller must not
    generate it again."""
    entries = [e for _, e in _entries(digest)]
    hashes = [evidence_hash(e) for e in entries]
    pending = set(range(len(entries)))
    for e in entries:
        e["suggestion"] = None

    def settle(i: int, row: dict | None) -> None:
        if row:
            remember(entries[i]["item_key"], hashes[i], row)
        entries[i]["suggestion"] = _public(row) if row else None
        pending.discard(i)

    async def batch() -> None:
        pairs = list(dict.fromkeys((e["item_key"], h) for e, h in zip(entries, hashes)))
        rows = await store.get_scrum_suggestions(pairs) if pairs else {}
        for i in list(pending):
            settle(i, rows.get((entries[i]["item_key"], hashes[i])))

    async def single(i: int) -> None:
        try:
            settle(i, await store.get_scrum_suggestion(entries[i]["item_key"], hashes[i]))
        except Exception:                                 # counted once below; stays pending
            failed.append(i)

    failed: list[int] = []
    started = time.monotonic()
    reason = None
    try:
        if hasattr(store, "get_scrum_suggestions"):
            await asyncio.wait_for(batch(), LOOKUP_DEADLINE_SECS)
        else:
            await asyncio.wait_for(asyncio.gather(*(single(i) for i in sorted(pending))), LOOKUP_DEADLINE_SECS)
    except asyncio.TimeoutError:
        reason = "passed the deadline"
    except Exception as exc:                              # the store is optional advice too
        reason = f"failed ({type(exc).__name__}: {exc})"
    if failed and reason is None:
        reason = f"failed for {len(failed)} items"
    took = time.monotonic() - started
    if pending:
        stale = 0
        for i in sorted(pending):
            row = _known(entries[i]["item_key"], hashes[i])
            if row is not None:
                entries[i]["suggestion"], stale = dict(row), stale + 1
        left = {entries[i]["item_key"] for i in pending if entries[i]["suggestion"] is None}
        log.warning("scrum suggestion lookup %s after %.1fs (%d items): %d served from last known, %d without a suggestion",
                    reason or "incomplete", took, len(entries), stale, len(left))
        if unresolved is not None:
            unresolved |= left
    elif took > SLOW_LOOKUP_LOG_SECS:
        log.warning("scrum suggestion lookup slow: %.1fs for %d items", took, len(entries))
    return digest


async def generate_missing(items: list[tuple[str, dict]], store: Any, call: Callable[[str], Awaitable[str | None]],
                           timeout: float | None = None, max_new: int | None = None) -> int:
    """Generate and store suggestions for `items` ((section, entry) pairs) that have none. Returns how many were stored."""
    todo = []
    for sec, e in items:
        key = (e["item_key"], evidence_hash(e))
        if key not in _IN_FLIGHT:
            _IN_FLIGHT.add(key)
            todo.append((sec, e, key))
    if max_new is not None and len(todo) > max_new:          # only callers that ask for a limit get one
        todo = _release(todo, max_new)

    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def one(sec: str, e: dict, key: tuple[str, str]) -> int:
        try:
            async with sem:
                raw = await asyncio.wait_for(call(PROMPT.format(item_key=e["item_key"], section=sec, evidence=evidence_text(e), hint=hint(e))), timeout or CALL_TIMEOUT_SECS)
            text = clean(raw)
            if not text:
                return 0
            await store.save_scrum_suggestion({"id": suggestion_id(*key), "item_key": key[0], "evidence_hash": key[1], "text": text,
                                               "model": MODEL, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            return 1
        except Exception as exc:                          # timeout, model error, store error: no suggestion
            log.warning("scrum suggestion for %s failed: %s", e["item_key"], type(exc).__name__)
            return 0
        finally:
            _IN_FLIGHT.discard(key)

    return sum(await asyncio.gather(*(one(*t) for t in todo)))


def _release(todo: list, keep: int) -> list:
    for _, _, key in todo[keep:]:
        _IN_FLIGHT.discard(key)
    return todo[:keep]


def refresh_async(digest: dict[str, Any], store: Any, call: Callable[[str], Awaitable[str | None]],
                  skip: set[str] | frozenset[str] = frozenset()) -> None:
    """Generate suggestions for entries whose `suggestion` is None in a background task; returns at once and never raises.
    Entries are copied, so the digest already handed to the caller is not touched. Call after attach_cached."""
    try:
        missing = [(sec, dict(e)) for sec, e in _entries(digest) if e.get("suggestion") is None and e["item_key"] not in skip]
        if not missing:
            return
        task = asyncio.get_running_loop().create_task(generate_missing(missing, store, call))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    except Exception as exc:
        log.warning("scrum suggestion refresh not started: %s", exc)
