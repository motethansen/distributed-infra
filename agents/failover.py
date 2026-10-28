#!/usr/bin/env python3
"""LLM failover helpers — decide when another agent should take over.

An agent failure is only handed to the next agent in the chain when it is an
*availability* failure: a usage/rate limit, an exhausted quota, an overloaded
provider, or an agent that is not installed / not logged in. Anything else (a
timeout, a real error in the work itself) still stops and waits for a human,
because retrying it on another provider would repeat side effects.

A limited agent is put on a cooldown until the reset time it reported (e.g.
"resets 8:50pm (Asia/Singapore)", "try again in 2 hours"), so the next task skips
it immediately instead of failing first. Cooldowns are per worker machine and
live in data/agent_cooldowns.json (gitignored).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

_REPO_ROOT = Path(__file__).resolve().parent.parent
COOLDOWN_FILE = Path(os.environ.get("AGENT_COOLDOWN_FILE", _REPO_ROOT / "data" / "agent_cooldowns.json"))
DEFAULT_COOLDOWN_SECS = int(os.environ.get("AGENT_LIMIT_COOLDOWN_SECS", "1800"))
MAX_COOLDOWN = timedelta(days=7)

# Usage limits, rate limits, quotas, overload — wording seen from claude, codex,
# Gemini/agy and the DeepSeek API.
_LIMIT_RE = re.compile(
    r"session limit|usage limit|weekly limit|daily limit|rate.?limit|limit (?:reached|exceeded)"
    r"|hit your (?:\w+ )?limit|out of (?:extra )?usage|quota|too many requests|\b429\b"
    r"|resource.?exhausted|insufficient[_ ](?:quota|balance)|credit balance is too low"
    r"|overloaded|\b529\b",
    re.IGNORECASE,
)

# The agent cannot run at all on this machine (missing CLI, logged out, no key).
_UNAVAILABLE_RE = re.compile(
    r"CLI not found|not logged in|please (?:run )?\S* ?login|invalid api key|authentication_error"
    r"|_API(?:_KEY)? not set|api key not set|unauthori[sz]ed|\b401\b",
    re.IGNORECASE,
)


def classify(error: str | None) -> str | None:
    """Return 'limit', 'unavailable', or None (= do not fail over)."""
    text = error or ""
    if _LIMIT_RE.search(text):
        return "limit"
    if _UNAVAILABLE_RE.search(text):
        return "unavailable"
    return None


# ── Reset-time parsing ────────────────────────────────────────────────────────
_CLOCK_RE = re.compile(
    r"(?:resets?|again at|available at|until)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
    r"(?:\s*\(([A-Za-z_]+/[A-Za-z_]+)\))?",
    re.IGNORECASE,
)
_UNIT_SECS = {"d": 86400, "day": 86400, "h": 3600, "hr": 3600, "hour": 3600,
              "m": 60, "min": 60, "minute": 60, "s": 1, "sec": 1, "second": 1}
_DUR_RE = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE)


def parse_reset(error: str, now: datetime | None = None) -> datetime | None:
    """Best-effort: when does the limit reported in `error` lift? None if unknown."""
    now = now or datetime.now(timezone.utc)
    text = error or ""

    m = re.search(r"(?:try again|retry|resets?|available again)\s+in\s+([^.\n]+)", text, re.IGNORECASE)
    if m:
        secs = 0
        for n, unit in _DUR_RE.findall(m.group(1)):
            u = unit.lower().rstrip("s") or unit.lower()
            secs += int(n) * _UNIT_SECS.get(u, _UNIT_SECS.get(u[:1], 0))
        if secs:
            return now + min(timedelta(seconds=secs), MAX_COOLDOWN)

    m = _CLOCK_RE.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower().replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        tz = timezone.utc
        if m.group(4) and ZoneInfo is not None:
            try:
                tz = ZoneInfo(m.group(4))
            except Exception:
                tz = timezone.utc
        elif not m.group(4):
            tz = datetime.now().astimezone().tzinfo or timezone.utc  # worker local time
        local_now = now.astimezone(tz)
        target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)
    return None


# ── Cooldown store ────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        with open(COOLDOWN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = COOLDOWN_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, COOLDOWN_FILE)
    except OSError:
        pass


def cooling_until(agent: str, now: datetime | None = None) -> datetime | None:
    """The time this agent's cooldown ends, or None if it is usable now."""
    now = now or datetime.now(timezone.utc)
    entry = _load().get(agent)
    if not entry:
        return None
    try:
        until = datetime.fromisoformat(entry["until"])
    except (KeyError, ValueError, TypeError):
        return None
    return until if until > now else None


def mark_limited(agent: str, error: str, now: datetime | None = None) -> datetime:
    """Record that `agent` hit a limit; returns when it becomes usable again."""
    now = now or datetime.now(timezone.utc)
    until = parse_reset(error, now) or now + timedelta(seconds=DEFAULT_COOLDOWN_SECS)
    data = {k: v for k, v in _load().items() if cooling_until(k, now)}  # drop expired
    data[agent] = {"until": until.isoformat(), "reason": (error or "")[:200], "since": now.isoformat()}
    _save(data)
    return until


def clear(agent: str) -> None:
    """Forget an agent's cooldown (it just succeeded)."""
    data = _load()
    if data.pop(agent, None) is not None:
        _save(data)


# ── Conversation hand-over ────────────────────────────────────────────────────
MAX_HISTORY_CHARS = int(os.environ.get("AGENT_HANDOVER_MAX_CHARS", "12000"))


def render_history(history: list[dict] | None) -> str:
    """Render prior turns as a plain transcript, newest kept when trimming."""
    lines = []
    for turn in history or []:
        role = "User" if turn.get("role") == "user" else f"Assistant ({turn.get('agent') or 'agent'})"
        text = str(turn.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    out = "\n\n".join(lines)
    if len(out) > MAX_HISTORY_CHARS:
        out = "…(earlier turns trimmed)\n\n" + out[-MAX_HISTORY_CHARS:]
    return out


def handover_prompt(prompt: str, history: list[dict] | None, previous_failures: list[dict]) -> str:
    """Prompt for an agent that takes over: transcript so far + the new message."""
    parts = []
    transcript = render_history(history)
    if transcript:
        parts.append("You are taking over an ongoing conversation from another AI assistant that is "
                     "temporarily unavailable. Here is the conversation so far:\n\n" + transcript)
    if previous_failures and not transcript:
        parts.append("Note: you are covering for another AI agent that is temporarily unavailable "
                     "(usage limit). It may already have started this task — if you are working in a "
                     "directory, check its current state before redoing anything.")
    parts.append(("The user now says:\n\n" if transcript else "") + prompt)
    return "\n\n".join(parts)


def missed_turns(history: list[dict] | None, agent: str) -> list[dict]:
    """Turns after the last reply `agent` itself gave (what its own session lacks)."""
    history = history or []
    last = -1
    for i, turn in enumerate(history):
        if turn.get("role") == "assistant" and turn.get("agent") == agent:
            last = i
    return history[last + 1:]


def has_answered(history: list[dict] | None, agent: str) -> bool:
    return any(t.get("role") == "assistant" and t.get("agent") == agent for t in history or [])
