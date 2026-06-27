#!/usr/bin/env python3
"""Model routing layer (Track #5).

Resolves (task_kind, sensitivity) — plus optional caller overrides — to a concrete
(agent, model) pair, so every call picks the cheapest provider that fits while a
hard privacy guard keeps sensitive data off non-private (cloud-CN) providers.

Policy lives in config/routing.yaml (fleet-wide, no secrets). Opus stays blocked
by claude_agent's own cost policy regardless of what is requested here.

CLI:
  python agents/router.py --task-kind code
  python agents/router.py --sensitivity private --agent deepseek   # guard reroutes
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_ROUTING_FILE = Path(__file__).parent.parent / "config" / "routing.yaml"

# Built-in fallback so the router still works if the yaml is missing/unreadable.
_DEFAULTS = {
    "default_class": "default",
    "classes": {
        "privacy": {"agent": "claude", "model": "sonnet"},
        "coding": {"agent": "claude", "model": "sonnet"},
        "planning": {"agent": "claude", "model": "sonnet"},
        "reasoning": {"agent": "deepseek", "model": "deepseek-reasoner"},
        "bulk": {"agent": "deepseek", "model": "deepseek-chat"},
        "mechanical": {"agent": "claude", "model": "haiku"},
        "default": {"agent": "claude", "model": "sonnet"},
    },
    "task_kinds": {},
    "privacy_classes": ["privacy"],
    "sensitive_values": ["private", "privacy", "sensitive", "personal", "confidential", "secret"],
    "non_private_agents": ["deepseek"],
    "privacy_fallback": {"agent": "claude", "model": "sonnet"},
}


def _load_policy() -> dict:
    try:
        with open(_ROUTING_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return _DEFAULTS
    # shallow-merge onto defaults so a partial yaml still has every key
    merged = dict(_DEFAULTS)
    merged.update(data)
    return merged


def route(task_kind: str | None = None, sensitivity: str | None = None,
          agent: str | None = None, model: str | None = None) -> tuple[str, str]:
    """Return (agent, model).

    - sensitivity in the sensitive set -> the `privacy` class (overrides task_kind).
    - otherwise task_kind -> class -> {agent, model}.
    - a caller-supplied agent/model overrides the class choice...
    - ...except the privacy guard: privacy/sensitive work is rerouted off any
      non-private provider to `privacy_fallback`, dropping the requested model.
    """
    p = _load_policy()
    classes = p.get("classes", {})
    kinds = p.get("task_kinds", {})
    privacy_classes = set(p.get("privacy_classes", ["privacy"]))
    sensitive_values = set(p.get("sensitive_values", []))
    non_private = set(p.get("non_private_agents", ["deepseek"]))
    fallback = p.get("privacy_fallback", {"agent": "claude", "model": "sonnet"})

    is_sensitive = (sensitivity or "").strip().lower() in sensitive_values

    if is_sensitive:
        cls = "privacy"
    else:
        cls = kinds.get((task_kind or "").strip().lower(), p.get("default_class", "default"))

    policy = classes.get(cls) or classes.get("default") or {"agent": "claude", "model": "sonnet"}

    # Per-call override wins for the normal path.
    policy_agent = (policy.get("agent", "claude") or "claude").strip().lower()
    chosen_agent = (agent or policy_agent).strip().lower()

    # Model resolution: explicit caller model wins; otherwise use the class model
    # ONLY when we're using the class's agent (a class model like "sonnet" is
    # provider-specific — don't hand it to an overridden agent like codex). An
    # overridden agent with no model falls back to that agent's own default.
    if model is not None:
        chosen_model = model.strip().lower()
    elif chosen_agent == policy_agent:
        chosen_model = (policy.get("model", "") or "").strip().lower()
    else:
        chosen_model = ""

    # Privacy guard (hard rule): sensitive / privacy-class data must never reach a
    # non-private provider — reroute to the fallback and drop the requested model
    # (it was likely for the wrong provider).
    if (is_sensitive or cls in privacy_classes) and chosen_agent in non_private:
        rerouted = fallback.get("agent", "claude")
        print(f"[router] privacy guard: '{chosen_agent}' is non-private; rerouting "
              f"{cls}/sensitivity={sensitivity!r} -> {rerouted}", flush=True)
        chosen_agent = rerouted
        chosen_model = (fallback.get("model", "sonnet") or "").strip().lower()

    return chosen_agent, chosen_model


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Resolve a routing decision")
    parser.add_argument("--task-kind", default=None)
    parser.add_argument("--sensitivity", default=None)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    a, m = route(task_kind=args.task_kind, sensitivity=args.sensitivity,
                 agent=args.agent, model=args.model)
    print(f"agent={a} model={m or '(agent default)'}")
