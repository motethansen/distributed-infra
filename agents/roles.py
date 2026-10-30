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

import re

import yaml

_ROLES_FILE = Path(__file__).parent.parent / "config" / "roles.yaml"


class RoleError(ValueError):
    """Unknown role, or a project that needs human approval first."""


# Prompts that look like they carry a credential never go to a non-private provider.
_SECRET = re.compile(
    r"(sk_(live|test)_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|"
    r"(?i:(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}))")


def looks_secret(text: str | None) -> bool:
    return bool(text and _SECRET.search(text))


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
            month_opus_cost: float = 0.0, policy: dict | None = None,
            prompt: str | None = None, approved: bool = False) -> dict:
    """Return {agent, model, allow_opus, note}. Raises RoleError for an unknown role or an isolated
    project without approval. An Opus role past its monthly cap runs on its `cap_fallback`."""
    p = policy if policy is not None else load_policy()
    roles = p.get("roles") or {}
    key = (role or "").strip().lower()
    spec = roles.get(key)
    if not spec:
        raise RoleError(f"unknown role {role!r}; known: {sorted(roles)}")
    isolated = {str(x).strip().lower() for x in (p.get("isolated_projects") or [])}
    if (project or "").strip().lower() in isolated and not approved:
        raise RoleError(f"project {project!r} is isolated: a human must approve every task "
                        f"(payload.approved: true)")
    agent = (spec.get("agent") or "claude").strip().lower()
    model = (spec.get("model") or "").strip().lower()
    note = ""

    if agent == "deepseek":
        allowed = {str(x).strip().lower() for x in (p.get("deepseek_projects") or [])}
        sensitive = (sensitivity or "").strip().lower() in {
            "private", "privacy", "sensitive", "personal", "confidential", "secret"}
        secret = looks_secret(prompt)
        if sensitive or secret or (project or "").strip().lower() not in allowed:
            fb = spec.get("private_fallback") or {"agent": "claude", "model": "sonnet"}
            agent, model = fb["agent"].strip().lower(), (fb.get("model") or "").strip().lower()
            note = ("deepseek not used: sensitive work" if sensitive
                    else "deepseek not used: the prompt looks like it carries a credential" if secret
                    else f"deepseek not used: project {project!r} is not in deepseek_projects")

    allow_opus = False
    if is_opus(model):
        if not spec.get("allow_opus"):
            raise RoleError(f"role {key!r} may not use Opus")
        cap = spec.get("monthly_cost_cap_usd")
        if cap is not None and month_opus_cost >= float(cap):
            fb = spec.get("cap_fallback")
            if not fb:
                raise RoleError(f"role {key!r}: monthly Opus cap reached "
                                f"(${month_opus_cost:.2f} of ${float(cap):.2f})")
            agent, model = fb["agent"].strip().lower(), (fb.get("model") or "").strip().lower()
            note = (f"opus cap reached (${month_opus_cost:.2f} of ${float(cap):.2f}); "
                    f"ran on {agent} {model}".strip())
        else:
            allow_opus = True
            warn = float(spec.get("warn_at") or 0)
            if cap is not None and warn and month_opus_cost >= warn * float(cap):
                note = f"opus at {100 * month_opus_cost / float(cap):.0f}% of the monthly cap"
    return {"agent": agent, "model": model, "allow_opus": allow_opus, "note": note}
