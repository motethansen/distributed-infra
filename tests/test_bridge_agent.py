"""Tests for the `agent <llm> <prompt>` WhatsApp command (BLI-050).

Covers the parser and the keyword→backend routing registry. The bridge lives at
services/whatsapp-bridge/bridge.py (not an importable package), so we load it by
file path.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_BRIDGE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "services" / "whatsapp-bridge" / "bridge.py"
)


def _load_bridge():
    spec = importlib.util.spec_from_file_location("wa_bridge", _BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = _load_bridge()


# ── Parsing ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,llm,prompt", [
    ("agent claude help me start a new writing project",
     "claude", "help me start a new writing project"),
    ("agent code produce an image of a car",
     "code", "produce an image of a car"),
    ("agent agy review my task list and suggest today's activities",
     "agy", "review my task list and suggest today's activities"),
    ("/agent codex fix this bug", "codex", "fix this bug"),
    ("AGENT Claude Be Loud", "claude", "Be Loud"),  # case-insensitive verb + llm
])
def test_parse_agent_command(text, llm, prompt):
    cmd, kwargs = bridge._parse(text)
    assert cmd == "agent"
    assert kwargs["llm"] == llm
    assert kwargs["prompt"] == prompt


def test_parse_agent_no_prompt():
    cmd, kwargs = bridge._parse("agent claude")
    assert cmd == "agent"
    assert kwargs["llm"] == "claude"
    assert kwargs["prompt"] == ""


def test_parse_bare_agent_keyword():
    cmd, kwargs = bridge._parse("agent")
    assert cmd == "agent"
    assert kwargs["llm"] == ""
    assert kwargs["prompt"] == ""


def test_agent_does_not_shadow_other_commands():
    # words that merely start with "agent" must not trigger the command
    assert bridge._parse("agential thinking")[0] != "agent"
    assert bridge._parse("status")[0] == "status"
    assert bridge._parse("run claude hi")[0] == "run"


# ── Registry / routing ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("keyword,backend", [
    ("claude", "claude"),
    ("code", "claude"),          # Claude Code is the claude CLI
    ("claude-code", "claude"),
    ("agy", "agy"),
    ("antigravity", "agy"),
    ("codex", "codex"),
    ("gpt", "codex"),
    ("groq", "groq"),
    ("content", "content"),
    ("social", "social"),
])
def test_alias_maps_to_backend(keyword, backend):
    assert bridge._AGENT_ALIASES[keyword] == backend


def test_unknown_keyword_not_in_registry():
    assert bridge._AGENT_ALIASES.get("banana") is None


def test_every_backend_is_a_real_worker_agent():
    # Backends must exist in the worker runner's AGENTS registry.
    from agents.runner import _load_agents, AGENTS
    _load_agents()
    for backend in set(bridge._AGENT_ALIASES.values()):
        assert backend in AGENTS, f"{backend} missing from worker AGENTS"


def test_agent_choices_lists_keywords():
    choices = bridge._agent_choices()
    for kw in ("claude", "code", "agy", "codex"):
        assert kw in choices
