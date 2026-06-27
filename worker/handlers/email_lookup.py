"""Handler for `email_lookup` tasks (#14) — read-only Gmail search over IMAP.

Uses an app password (no OAuth): set on the machine that runs this handler
  GMAIL_IMAP_USER=you@gmail.com
  GMAIL_IMAP_APP_PASSWORD=xxxxxxxxxxxxxxxx   # 16-char app password (2FA required)

payload:
  query: str   — optional Gmail search (X-GM-RAW syntax, e.g. "from:bank is:unread",
                 "invoice newer_than:7d"). Empty -> most recent inbox messages.
  limit: int   — max messages to return (default 5, capped at 20).
  days:  int   — when no query, look back this many days (default 7).

Read-only: the mailbox is opened readonly; sending is a separate v2 needs_human
action. Privacy: email is privacy-class (#5) — summarize via Claude only.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import os
from email.header import decode_header
from email.utils import parseaddr

from shared.models import Task

_HOST = os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com")
_MAX_LIMIT = 20


def _decode(value: str | bytes | None) -> str:
    """Decode a possibly RFC2047-encoded header to a plain string."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    parts = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            parts.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(text)
    return "".join(parts).strip()


def _snippet(msg: email.message.Message, limit: int = 160) -> str:
    """Best-effort short plain-text preview of a message."""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                    payload = part.get_payload(decode=True) or b""
                    text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
            else:
                text = ""
        else:
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        text = ""
    text = " ".join(text.split())
    return text[:limit]


def _search(user: str, password: str, query: str, limit: int, days: int) -> list[dict]:
    """Blocking IMAP search; run via asyncio.to_thread."""
    imap = imaplib.IMAP4_SSL(_HOST)
    try:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        if query:
            typ, data = imap.search(None, "X-GM-RAW", query)
        else:
            # last `days` days, most recent first
            import datetime
            since = (datetime.date.today() - datetime.timedelta(days=max(days, 1))).strftime("%d-%b-%Y")
            typ, data = imap.search(None, f'(SINCE "{since}")')
        ids = (data[0].split() if data and data[0] else [])
        ids = ids[-limit:][::-1]  # most recent `limit`, newest first
        out = []
        for mid in ids:
            typ, msg_data = imap.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            name, addr = parseaddr(_decode(msg.get("From")))
            out.append({
                "from": name or addr,
                "from_addr": addr,
                "subject": _decode(msg.get("Subject")) or "(no subject)",
                "date": _decode(msg.get("Date")),
                "snippet": _snippet(msg),
            })
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _format(query: str, msgs: list[dict]) -> str:
    if not msgs:
        return f"📭 No emails found{f' for: {query}' if query else ''}."
    head = f"📧 {len(msgs)} email(s)" + (f' for "{query}"' if query else " (recent)")
    lines = [head, ""]
    for m in msgs:
        lines.append(f"• {m['from']} — {m['subject']}")
        if m.get("date"):
            lines.append(f"  {m['date']}")
        if m.get("snippet"):
            lines.append(f"  {m['snippet']}")
    return "\n".join(lines)


async def handle_email_lookup(task: Task) -> dict:
    payload = task.payload or {}
    query = (payload.get("query") or "").strip()
    limit = min(int(payload.get("limit", 5) or 5), _MAX_LIMIT)
    days = int(payload.get("days", 7) or 7)

    user = os.getenv("GMAIL_IMAP_USER", "")
    password = os.getenv("GMAIL_IMAP_APP_PASSWORD", "")
    if not user or not password:
        return {
            "needs_human": True,
            "notes": "GMAIL_IMAP_USER / GMAIL_IMAP_APP_PASSWORD not set on this machine.",
            "action": "Add a Gmail app password (2FA → App passwords) to the worker .env, then retry.",
        }

    try:
        msgs = await asyncio.to_thread(_search, user, password, query, limit, days)
    except imaplib.IMAP4.error as exc:
        return {"needs_human": True, "notes": f"IMAP login/search failed: {exc}",
                "action": "Check GMAIL_IMAP_USER and the app password are correct and 2FA is on."}
    except Exception as exc:
        return {"error": f"email lookup failed: {exc}"}

    return {"response": _format(query, msgs), "count": len(msgs)}
