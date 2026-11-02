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
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from shared.scrum_digest import evidence_text

log = logging.getLogger(__name__)

MAX_CHARS = 300
MAX_NEW_PER_LOAD = 6          # new model calls per board load; the rest fill in on the next load
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


async def attach_cached(digest: dict[str, Any], store: Any) -> dict[str, Any]:
    """Set `suggestion` on every review/decision/adjustment entry from the store only: the stored row or None.
    Never calls the model. In place; returned for convenience. `store` has get_scrum_suggestion(item_key, hash)."""
    for _, e in _entries(digest):
        try:
            row = await store.get_scrum_suggestion(e["item_key"], evidence_hash(e))
        except Exception as exc:                          # the store is optional advice too
            log.warning("scrum suggestion lookup failed for %s: %s", e["item_key"], exc)
            row = None
        e["suggestion"] = _public(row) if row else None
    return digest


async def generate_missing(items: list[tuple[str, dict]], store: Any, call: Callable[[str], Awaitable[str | None]],
                           timeout: float | None = None, max_new: int = MAX_NEW_PER_LOAD) -> int:
    """Generate and store suggestions for `items` ((section, entry) pairs) that have none. Returns how many were stored."""
    todo = []
    for sec, e in items:
        key = (e["item_key"], evidence_hash(e))
        if key not in _IN_FLIGHT:
            _IN_FLIGHT.add(key)
            todo.append((sec, e, key))
    todo = todo[:max_new] if len(todo) <= max_new else _release(todo, max_new)

    async def one(sec: str, e: dict, key: tuple[str, str]) -> int:
        try:
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


def refresh_async(digest: dict[str, Any], store: Any, call: Callable[[str], Awaitable[str | None]]) -> None:
    """Generate suggestions for entries whose `suggestion` is None in a background task; returns at once and never raises.
    Entries are copied, so the digest already handed to the caller is not touched. Call after attach_cached."""
    try:
        missing = [(sec, dict(e)) for sec, e in _entries(digest) if e.get("suggestion") is None]
        if not missing:
            return
        task = asyncio.get_running_loop().create_task(generate_missing(missing, store, call))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    except Exception as exc:
        log.warning("scrum suggestion refresh not started: %s", exc)
