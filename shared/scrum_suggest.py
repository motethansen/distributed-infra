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

Reply with the sentence only."""


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


async def attach(digest: dict[str, Any], store: Any, call: Callable[[str], Awaitable[str | None]],
                 timeout: float = CALL_TIMEOUT_SECS, max_new: int = MAX_NEW_PER_LOAD) -> dict[str, Any]:
    """Fill `suggestion` on every review/decision/adjustment entry of `digest` (in place; returned for convenience).

    `store` has get_scrum_suggestion(item_key, hash) and save_scrum_suggestion(row); `call(prompt)` returns the model text."""
    work = [(sec, e) for sec, key in (("review", "review_queue"), ("decision", "decisions"), ("adjustment", "adjustments"))
            for e in digest.get(key) or [] if isinstance(e, dict) and e.get("item_key")]
    missing: list[tuple[str, dict, str]] = []
    for sec, e in work:
        h = evidence_hash(e)
        try:
            row = await store.get_scrum_suggestion(e["item_key"], h)
        except Exception as exc:                          # the store is optional advice too
            log.warning("scrum suggestion lookup failed for %s: %s", e["item_key"], exc)
            row = None
        if row:
            e["suggestion"] = _public(row)
        else:
            e["suggestion"] = None
            missing.append((sec, e, h))

    async def one(sec: str, e: dict, h: str) -> None:
        try:
            raw = await asyncio.wait_for(call(PROMPT.format(item_key=e["item_key"], section=sec, evidence=evidence_text(e))), timeout)
            text = clean(raw)
            if not text:
                return
            row = {"id": suggestion_id(e["item_key"], h), "item_key": e["item_key"], "evidence_hash": h, "text": text,
                   "model": MODEL, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            await store.save_scrum_suggestion(row)
            e["suggestion"] = _public(row)
        except Exception as exc:                          # timeout, model error, store error: no suggestion, board loads
            log.warning("scrum suggestion for %s failed: %s", e["item_key"], type(exc).__name__)

    if missing[:max_new]:
        await asyncio.gather(*(one(*m) for m in missing[:max_new]))
    return digest
