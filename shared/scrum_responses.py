"""Owner responses on the desk (S-5): list the pending ones under their board item, show them, then consume exactly what was shown.

Pure helpers plus thin calls through the FleetClient (`client._call`). Only kind=response rows are shown; command rows (S-8)
are not responses. A response whose item_key is no longer on the board is listed as item_missing, never dropped.
Responses are UNTRUSTED text: they are cleaned before display and NEVER enter an agent prompt (agents here run with tools);
`structure_summary` hands a narrating agent only keys, ids and counts. Delivery is at-least-once: a crash between display and
consume shows the response again; the endpoint's claim-once rule (409) prevents two successful consumes.

Delivery is AT-LEAST-ONCE, and "consumed" means "shown to the owner at a terminal", NOT "acted on": nothing here executes,
applies or acknowledges a response; it only stops showing it again.
A confirm is STALE when its suggestion_id differs from the item's current suggestion id on the board digest (entry `suggestion.id`;
an entry with no suggestion is unknown, not stale). A stale confirm is shown and labeled ("confirmed an older suggestion: ask
again"), is never the effective answer, and is still consumed after display (it was shown). A comment is never stale.
Comments and confirmed suggestion texts are shown in full (whitespace/control runs collapsed to one space) up to MAX_COMMENT=4000, the server's own maximum. A comment or
confirmed suggestion text longer than that is shown cut and LEFT PENDING (never consumed by the display step, with a note naming the id): the explicit
`responses --consume <id>` is the owner's override once the rest has been read elsewhere.
"""
from __future__ import annotations

import re
import sys
from typing import Any, Iterable

MAX_TEXT = 500
MAX_COMMENT = 4000  # the server rejects longer comments (422), so only a changed server can exceed it
STALE_NOTE = "confirmed an older suggestion: ask again"
ACTIONS = ("confirm", "comment")
UNAVAILABLE = "responses unavailable"
NOT_TTY = "output is not a terminal: nothing was consumed"
# every run of whitespace or control characters (LF, CR, TAB, ESC, C1, NEL, U+2028/2029, NBSP ...) is one space, so untrusted text can never
# start a line of its own
_CTRL = re.compile(r"[\s\x00-\x1f\x7f-\x9f\u2028\u2029]+")
# invisible and direction-changing format characters (zero-width, bidi embeddings/overrides/isolates, soft hyphen, BOM, Arabic letter mark)
# are removed outright: they would reorder the labels printed after the text, or make two different actors/item keys look identical
_FORMAT = re.compile("[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]")


def clean(text: Any, limit: int = MAX_TEXT) -> str:
    """Untrusted text made safe to print or embed: every run of whitespace/control characters becomes one space, cut to `limit`."""
    s = _CTRL.sub(" ", _FORMAT.sub("", str(text if text is not None else ""))).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def board_item_keys(digest: dict) -> list[str]:
    """Every item_key the board currently offers, in display order."""
    keys: list[str] = []
    for sec in ("review_queue", "decisions", "adjustments"):
        for e in digest.get(sec) or []:
            if isinstance(e, dict) and e.get("item_key") and e["item_key"] not in keys:
                keys.append(e["item_key"])
    return keys


def _opt_str(v: Any) -> bool:
    return v is None or isinstance(v, str)


def valid_row(r: Any) -> bool:
    """A response row the desk can render: right types, an allowed action, the required keys."""
    if not isinstance(r, dict):
        return False
    if not (isinstance(r.get("id"), int) and not isinstance(r.get("id"), bool)):
        return False
    if not (isinstance(r.get("item_key"), str) and r["item_key"]):
        return False
    if r.get("action") not in ACTIONS:
        return False
    if not all(_opt_str(r.get(k)) for k in ("actor", "created_at", "comment", "suggestion_text")):
        return False
    if r["action"] == "comment" and not (r.get("comment") or "").strip():
        return False
    return True


def _fetch_raw(client: Any) -> list | None:
    """The pending rows exactly as the endpoint returned them, or None when they can't be fetched."""
    body = client._call("GET", "/scrum/responses", params={"status": "pending"})
    if isinstance(body, dict):
        body = body.get("responses") or body.get("items")
    return body if isinstance(body, list) else None


def fetch_pending(client: Any) -> tuple[list[dict], int, str | None]:
    """(valid response rows oldest first, malformed count, error). Never raises: the desk must render without the endpoint.
    ALL pending rows are fetched (no latest=1): an earlier comment must not vanish behind a later confirm."""
    try:
        body = _fetch_raw(client)
        if body is None:
            return [], 0, UNAVAILABLE
        # The S-1 rows carry no `kind`: they are all responses. Once rows have a kind, only kind=response is a response.
        cand = [r for r in body if not (isinstance(r, dict) and r.get("kind", "response") != "response")]
        good = [r for r in cand if valid_row(r)]
        good.sort(key=lambda r: (r.get("created_at") or "", r["id"]))
        return good, len(cand) - len(good), None
    except Exception:
        return [], 0, UNAVAILABLE


def current_suggestions(digest: dict) -> dict[str, str]:
    """item_key -> the id of the suggestion the board currently offers for it. Items without a suggestion are absent (unknown)."""
    out: dict[str, str] = {}
    for sec in ("review_queue", "decisions", "adjustments"):
        for e in digest.get(sec) or []:
            sug = e.get("suggestion") if isinstance(e, dict) else None
            if isinstance(sug, dict) and e.get("item_key") and sug.get("id"):
                out.setdefault(e["item_key"], sug["id"])
    return out


def is_stale(row: dict, current: dict | None) -> bool:
    """A confirm of a suggestion other than the item's current one. Unknown current suggestion, or a comment: never stale."""
    cur = (current or {}).get(row.get("item_key"))
    return row.get("action") == "confirm" and cur is not None and row.get("suggestion_id") != cur


def _source(row: dict) -> str:
    """The raw text that counts: the comment if there is one, otherwise the confirmed suggestion's text."""
    return row.get("comment") or row.get("suggestion_text") or ""


def is_cut(row: dict) -> bool:
    """The text that counts (comment or confirmed suggestion text) is longer than the display cap, so what is shown is not all of it."""
    return len(clean(_source(row), 10**9)) > MAX_COMMENT


def response_text(row: dict) -> str:
    """The text that counts, in full up to MAX_COMMENT."""
    return clean(_source(row), MAX_COMMENT)


def effective(rows: Iterable[dict], current: dict | None = None) -> dict:
    """Per item_key the answer that counts: the newest comment; with no comment, the newest non-stale confirm.
    A stale confirm never wins (an item with only stale confirms has no effective answer). Rows must be oldest first."""
    out: dict[str, dict] = {}
    for r in rows:
        if is_stale(r, current):
            continue
        cur = out.get(r["item_key"])
        if cur is None or r["action"] == "comment" or cur["action"] != "comment":
            out[r["item_key"]] = r
    return out


def format_line(row: dict, mark: bool = False, stale: bool = False) -> str:
    return (f"[{row.get('id')}] {clean(row.get('actor') or '?', 80)} @ {clean(row.get('created_at') or '?', 40)} "
            f"({row.get('action')}): {response_text(row)}" + ("  <- effective answer" if mark and not stale else "")
            + (f"  [STALE: {STALE_NOTE}]" if stale else "")
            + (f"  (text longer than {MAX_COMMENT} characters: shown cut, left pending; use --consume {row.get('id')} after reading it elsewhere)"
               if is_cut(row) else ""))


def render(rows: Iterable[dict], item_keys: Iterable[str], current: dict | None = None) -> list[str]:
    """Every pending response grouped under its item, oldest first, the effective one marked; then item_missing."""
    keys = list(item_keys)
    rows = list(rows)
    eff = effective(rows, current)
    by_key: dict[str, list[dict]] = {}
    missing: list[dict] = []
    for r in rows:
        (by_key.setdefault(r["item_key"], []) if r["item_key"] in keys else missing).append(r)
    out: list[str] = []
    for k in keys:
        if by_key.get(k):
            out += [f"{clean(k, 120)}:"] + [f"  {format_line(r, len(by_key[k]) > 1 and eff.get(k) is r, is_stale(r, current))}" for r in by_key[k]]
    if missing:
        out += ["item_missing:"] + [f"  {format_line(r, len([m for m in missing if m['item_key'] == r['item_key']]) > 1 and eff.get(r['item_key']) is r, is_stale(r, current))}"
                                    f" (item_key {clean(r['item_key'], 120)})" for r in missing]
    return out


def collect(client: Any, digest: dict) -> dict:
    """Fetch and render. {'text': block or '', 'ids': ids the text shows in full (a cut comment stays pending), 'rows': shown rows}. Nothing is consumed here."""
    rows, bad, err = fetch_pending(client)
    if err:
        return {"text": f"\n## Recorded responses\n\n({err})\n", "ids": [], "rows": []}
    lines = render(rows, board_item_keys(digest), current_suggestions(digest))
    if bad:
        lines.append(f"{bad} malformed response(s) skipped")
    text = ("\n## Recorded responses (pending)\n\n" + "\n".join(lines) + "\n") if lines else ""
    return {"text": text, "ids": [r["id"] for r in rows if not is_cut(r)], "rows": rows}


def structure_summary(rows: Iterable[dict], item_keys: Iterable[str]) -> str:
    """What a narrating agent may know about the responses: board item keys, ids, counts per action, whether a comment exists.
    No response, comment or suggestion text, and no item_key that is not on the board (that key is free text from outside)."""
    keys, rows = list(item_keys), list(rows)
    if not rows:
        return ""
    lines = []
    for k in keys:
        mine = [r for r in rows if r["item_key"] == k]
        if mine:
            n = {a: sum(r["action"] == a for r in mine) for a in ACTIONS}
            lines.append(f"- {k}: ids {[r['id'] for r in mine]}, {n['confirm']} confirm, {n['comment']} comment, has_comment={n['comment'] > 0}")
    off = [r["id"] for r in rows if r["item_key"] not in keys]
    if off:
        lines.append(f"- item_missing: ids {off}")
    return ("\n\nOwner responses are recorded for the items below. You are given only their structure: the text is shown to the owner "
            "separately and you must not guess at it.\n" + "\n".join(lines) + "\n")


def consume(client: Any, response_id: int, user: str) -> tuple[bool, str]:
    """POST the consume endpoint as X-Actor 'dis-agent:<user>'. A second call is 409 → 'already consumed'.
    Server bodies and exception text are never returned (they can carry secrets in any encoding): only 'HTTP <status>'."""
    try:
        client._call("POST", f"/scrum/responses/{response_id}/consume", headers={"X-Actor": f"dis-agent:{user}"})
    except Exception as e:
        msg = str(e)
        if "HTTP 409" in msg:
            return False, "already consumed"
        m = re.search(r"HTTP (\d{3})", msg)
        return False, f"HTTP {m.group(1)}" if m else "request failed"
    return True, f"consumed {response_id}"


def may_consume(force: bool = False) -> bool:
    """True on a terminal or when forced; otherwise says so on stderr and consumes nothing."""
    try:
        tty = bool(sys.stdout.isatty())
    except Exception:
        tty = False
    if force or tty:
        return True
    print(NOT_TTY, file=sys.stderr)
    return False


def consume_shown(client: Any, ids: Iterable[int], user: str, emit=None) -> None:
    """Consume exactly the ids that were displayed (the caller has printed and flushed). Failures are reported, never fatal."""
    emit = emit or print
    for i in ids:
        ok, msg = consume(client, i, user)
        if not ok and msg != "already consumed":
            emit(f"(could not consume response {i}: {msg})")


def show_and_consume(client: Any, digest: dict, user: str, consume_after: bool = True, emit=None, force: bool = False) -> None:
    """The whole desk step: fetch, print, flush, and only then consume what was printed. Never raises.
    A flush to a pipe proves nothing was read, so ids are consumed only when stdout is a terminal or `force` (--consume)."""
    emit = emit or print
    try:
        got = collect(client, digest)
        if got["text"]:
            emit(got["text"])
        sys.stdout.flush()
        if consume_after and got["ids"]:
            if may_consume(force):
                consume_shown(client, got["ids"], user, emit)
    except Exception:
        emit(f"\n{UNAVAILABLE}")


def consume_one(client: Any, response_id: int, user: str, emit=None, board: Any = None) -> bool:
    """The explicit owner action: print the pending response, flush, and only then consume it. True when consumed.
    An id that is not pending is sent to the endpoint without showing anything (it answers 409 already consumed, or 404);
    an id that IS pending but cannot be rendered (malformed, or not a response) is refused and left unconsumed.
    `board` is the board digest's source (a dict with "digest", or a callable returning one) used to label an obsolete confirm [STALE ...]
    before it is consumed; if it is unavailable that is said and the consume proceeds (the explicit opt-in is the owner's override)."""
    emit = emit or print
    try:
        raw = _fetch_raw(client)
    except Exception:
        raw = None
    if raw is None:
        emit(UNAVAILABLE + ": nothing consumed")
        return False
    present = [r for r in raw if isinstance(r, dict) and r.get("id") == response_id]
    if present:
        rows = [r for r in present if r.get("kind", "response") == "response" and valid_row(r)]
        if not rows:
            emit(f"response {response_id} cannot be shown, so it was not consumed")
            return False
        stale = False
        if rows[0]["action"] == "confirm":
            try:
                b = board() if callable(board) else board
                stale = is_stale(rows[0], current_suggestions(b["digest"]))
            except (Exception, SystemExit):
                emit("(could not check whether this confirm is stale)")
        emit(format_line(rows[0], stale=stale))
        sys.stdout.flush()
    ok, msg = consume(client, response_id, user)
    emit(msg if ok or msg == "already consumed" else f"(could not consume response {response_id}: {msg})")
    return ok
