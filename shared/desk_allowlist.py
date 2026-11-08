"""Desk command allowlist (S-8): the single source of truth for which verbs the desk may carry, and at what tier.

Dependency-free (stdlib only) so a worker can import it and re-check a command independently of the route.
Tiers: T1 runs (allowed this sprint), T2 needs an explicit owner confirmation, T3 is blocked outright.
The `actor` on a command is ASSERTED by the key holder (as in S-1), not authenticated: it is a label for the audit trail,
never proof of who sent it.
Unknown verbs: `check_verb` RAISES ValueError (the route maps that to 422); `tier_of` returns None instead.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

ALLOWLIST: dict[str, dict[str, Any]] = {
    "refresh_proposal": {"tier": "T1", "required": ()},
    "plan": {"tier": "T1", "required": ()},
    "validate": {"tier": "T1", "required": ()},
    "step_done": {"tier": "T1", "required": ("evidence",)},
    "approve": {"tier": "T2", "required": ()},
    "go": {"tier": "T2", "required": ()},
    "po_request": {"tier": "T2", "required": ()},
    "po_revise": {"tier": "T2", "required": ()},
    "accept": {"tier": "T2", "required": ()},
    "merge": {"tier": "T3", "required": ()},
    "deploy": {"tier": "T3", "required": ()},
    "cancel": {"tier": "T3", "required": ()},
    "close": {"tier": "T3", "required": ()},
}

# Verbs only the orchestrator itself may queue, never the page: `accept_confirmed` is queued by POST /scrum/accept after the
# preview digest matched and every PR was seen merged; `queue_story` by recording a confirm on a "the team should ..." suggestion.
# They are deliberately NOT in ALLOWLIST, so POST /scrum/responses answers 422 "Unknown verb" for them.
INTERNAL_VERBS = {"accept_confirmed": ("story_id", "plan_digest"), "queue_story": ("evidence",)}

ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
_ID_RE = re.compile(ID_PATTERN)
ALLOWED_TIERS_THIS_SPRINT = {"T1"}


def is_valid_id(value: Any) -> bool:
    """True for a str that matches ID_PATTERN in full (a trailing newline does not match)."""
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def tier_of(verb: Any) -> str | None:
    """The verb's tier, or None for an unknown (or non-string) verb."""
    entry = ALLOWLIST.get(verb) if isinstance(verb, str) else None
    return entry["tier"] if entry else None


def check_verb(verb: Any) -> tuple[str, str]:
    """('ok', tier) for T1, ('confirm', tier) for T2, ('blocked', tier) for T3. Raises ValueError for an unknown verb."""
    tier = tier_of(verb)
    if tier is None:
        raise ValueError("unknown verb")  # deliberately does not echo the (untrusted) verb
    if tier in ALLOWED_TIERS_THIS_SPRINT:
        return ("ok", tier)
    return ("confirm", tier) if tier == "T2" else ("blocked", tier)


def missing_required(verb: Any, fields: Mapping[str, Any]) -> list[str]:
    """Required fields of `verb` that are absent, None, or empty/whitespace-only. Unknown verb -> []."""
    entry = ALLOWLIST.get(verb) if isinstance(verb, str) else None
    if not entry:
        return []
    missing = []
    for name in entry["required"]:
        value = fields.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def is_internal(verb: Any) -> bool:
    return isinstance(verb, str) and verb in INTERNAL_VERBS
