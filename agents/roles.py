"""Scrum role -> agent/model, with the two guards the roster needs.

  * Opus is refused everywhere except a role marked `allow_opus`, and only while the month's
    Opus spend is under that role's cap.
  * A role that would run DeepSeek (a non-private provider) only does so for projects listed in
    `deepseek_projects` and never for sensitive work; otherwise it uses `private_fallback`.

Policy lives in config/roles.yaml. The orchestrator calls `resolve` when a task is queued and
stamps the outcome on the payload, so the worker never has to trust the caller.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLES_FILE = Path(__file__).parent.parent / "config" / "roles.yaml"


class RoleError(ValueError):
    """Unknown role, or the role's budget is used up."""


def load_policy() -> dict:
    try:
        with open(_ROLES_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def role_names() -> list[str]:
    return sorted((load_policy().get("roles") or {}).keys())


def is_opus(model: str | None) -> bool:
    return "opus" in (model or "").lower()


def resolve(role: str, project: str | None = None, sensitivity: str | None = None,
            month_opus_cost: float = 0.0, policy: dict | None = None) -> dict:
    """Return {agent, model, allow_opus, note}. Raises RoleError for an unknown role or when an
    Opus role has used up its monthly cap."""
    p = policy if policy is not None else load_policy()
    roles = p.get("roles") or {}
    key = (role or "").strip().lower()
    spec = roles.get(key)
    if not spec:
        raise RoleError(f"unknown role {role!r}; known: {sorted(roles)}")
    agent = (spec.get("agent") or "claude").strip().lower()
    model = (spec.get("model") or "").strip().lower()
    note = ""

    if agent == "deepseek":
        allowed = {str(x).strip().lower() for x in (p.get("deepseek_projects") or [])}
        sensitive = (sensitivity or "").strip().lower() in {
            "private", "privacy", "sensitive", "personal", "confidential", "secret"}
        if sensitive or (project or "").strip().lower() not in allowed:
            fb = spec.get("private_fallback") or {"agent": "claude", "model": "sonnet"}
            agent, model = fb["agent"].strip().lower(), (fb.get("model") or "").strip().lower()
            note = ("deepseek not used: sensitive work" if sensitive
                    else f"deepseek not used: project {project!r} is not in deepseek_projects")

    allow_opus = False
    if is_opus(model):
        if not spec.get("allow_opus"):
            raise RoleError(f"role {key!r} may not use Opus")
        cap = spec.get("monthly_cost_cap_usd")
        if cap is not None and month_opus_cost >= float(cap):
            raise RoleError(f"role {key!r}: monthly Opus cap reached "
                            f"(${month_opus_cost:.2f} of ${float(cap):.2f})")
        allow_opus = True
    return {"agent": agent, "model": model, "allow_opus": allow_opus, "note": note}
