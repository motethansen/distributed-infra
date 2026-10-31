"""One ordered list of LLM choices for clients, and what each choice sends.

Pure: no env access, no secrets, no I/O. Role entries come from the roles mapping passed in
(config/roles.yaml), so adding a role there adds a choice with no code change.
"""
from __future__ import annotations

from typing import Any

_MAX_STR = 200
_PAYLOAD_KEYS = {"agent", "model", "role"}


def _short(value: Any) -> str:
    return value.strip()[:_MAX_STR] if isinstance(value, str) else ""


def _payload(**kw: str) -> dict[str, str]:
    return {k: _short(v) for k, v in kw.items() if k in _PAYLOAD_KEYS and isinstance(v, str)}


def _choice(id_: str, label: str, kind: str, payload: dict[str, str], note: str) -> dict[str, Any]:
    return {"id": id_, "label": label, "kind": kind, "payload": payload, "note": _short(note)}


STATIC_CHOICES: list[dict[str, Any]] = [
    _choice("auto", "Auto", "auto", {}, "Router default: claude/sonnet."),
    _choice("claude", "Claude", "agent", _payload(agent="claude"), "Claude agent, default model."),
    _choice("codex", "Codex", "agent", _payload(agent="codex"), "Codex agent."),
    _choice("agy", "Agy", "agent", _payload(agent="agy"), "Agy agent."),
    _choice("deepseek", "DeepSeek", "agent", _payload(agent="deepseek"),
            "Rerouted to claude/sonnet for unlisted projects or secret-looking prompts."),
]


def build_llm_choices(roles: dict[str, Any]) -> list[dict[str, Any]]:
    """Static choices, then one `role:<name>` entry per role in the file. Opus is reachable only as role:product_owner."""
    out = [{**c, "payload": dict(c["payload"])} for c in STATIC_CHOICES]
    for name, spec in (roles or {}).items():
        if not isinstance(name, str) or not name:
            continue
        note = spec.get("best_for") if isinstance(spec, dict) else ""
        out.append(_choice(f"role:{name}"[:_MAX_STR], name.replace("_", " ").title(), "role",
                           _payload(role=name), note))
    return out
