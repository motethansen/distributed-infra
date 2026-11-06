"""Owner responses on the desk (S-5): list the pending ones under their board item, and mark one consumed.

Pure helpers plus two thin calls through the FleetClient (`client._call`). Only kind=response rows are shown; command
rows (S-8) are not responses. A response whose item_key is no longer on the board is listed as item_missing, never dropped.
"""
from __future__ import annotations

from typing import Any, Iterable


def board_item_keys(digest: dict) -> list[str]:
    """Every item_key the board currently offers, in display order."""
    keys: list[str] = []
    for sec in ("review_queue", "decisions", "adjustments"):
        for e in digest.get(sec) or []:
            if isinstance(e, dict) and e.get("item_key") and e["item_key"] not in keys:
                keys.append(e["item_key"])
    return keys


def fetch_pending(client: Any) -> tuple[list[dict], str | None]:
    """(response rows, error). Never raises: the desk must still render when the responses endpoint is unavailable."""
    try:
        body = client._call("GET", "/scrum/responses", params={"status": "pending", "latest": 1})
    except Exception as e:
        return [], f"responses unavailable ({str(e)[:100]})"
    if isinstance(body, dict):
        body = body.get("responses") or body.get("items")
    if not isinstance(body, list):
        return [], "responses unavailable (unexpected reply)"
    # The S-1 rows carry no `kind`: they are all responses. Once rows have a kind, only kind=response is a response.
    return [r for r in body if isinstance(r, dict) and r.get("kind", "response") == "response"], None


def response_text(row: dict) -> str:
    """The text that counts: the comment if there is one, otherwise the confirmed suggestion's text."""
    return (row.get("comment") or "").strip() or (row.get("suggestion_text") or "").strip()


def format_line(row: dict) -> str:
    return f"[{row.get('id')}] {row.get('actor') or '?'} @ {row.get('created_at') or '?'} ({row.get('action') or '?'}): {response_text(row)}"


def render(rows: Iterable[dict], item_keys: Iterable[str]) -> list[str]:
    """Responses grouped under their item, then an item_missing block for those whose item left the board."""
    keys = list(item_keys)
    by_key: dict[str, list[dict]] = {}
    missing: list[dict] = []
    for r in rows:
        (by_key.setdefault(r.get("item_key"), []) if r.get("item_key") in keys else missing).append(r)
    out: list[str] = []
    for k in keys:
        if by_key.get(k):
            out += [f"{k}:"] + [f"  {format_line(r)}" for r in by_key[k]]
    if missing:
        out += ["item_missing:"] + [f"  {format_line(r)} (item_key {r.get('item_key')})" for r in missing]
    return out


def section(client: Any, digest: dict) -> str:
    """The 'Recorded responses' block for the desk commands ('' when there is nothing to say)."""
    rows, err = fetch_pending(client)
    if err:
        return f"\n## Recorded responses\n\n({err})\n"
    lines = render(rows, board_item_keys(digest))
    return ("\n## Recorded responses (pending)\n\n" + "\n".join(lines) + "\n") if lines else ""


def consume(client: Any, response_id: int, user: str) -> tuple[bool, str]:
    """POST the existing consume endpoint as X-Actor 'dis-agent:<user>'. A second call is 409 → 'already consumed'."""
    try:
        client._call("POST", f"/scrum/responses/{response_id}/consume", headers={"X-Actor": f"dis-agent:{user}"})
    except Exception as e:
        msg = str(e)
        if "HTTP 409" in msg:
            return False, "already consumed"
        return False, msg[:200]
    return True, f"consumed {response_id}"
