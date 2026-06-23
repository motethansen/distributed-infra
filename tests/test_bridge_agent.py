"""Tests for the `agent <llm> <prompt>` WhatsApp command (BLI-050).

Covers the parser and the keyword→backend routing registry. The bridge lives at
services/whatsapp-bridge/bridge.py (not an importable package), so we load it by
file path.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import time

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


# ── Multi-turn sessions ──────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["end", "/end", "reset", "/reset", "new", "/new", "END"])
def test_parse_end_session(text):
    assert bridge._parse(text)[0] == "end_session"


def test_only_claude_is_resumable():
    # claude keeps a conversation; agy/codex run one-shot
    assert "claude" in bridge._RESUMABLE_BACKENDS
    assert "agy" not in bridge._RESUMABLE_BACKENDS
    assert "codex" not in bridge._RESUMABLE_BACKENDS


def test_live_session_returns_active_then_expires():
    bridge._sessions.clear()
    chat = "me@c.us"
    bridge._sessions[chat] = {"agent": "claude", "llm": "claude",
                              "session_id": "sid-1", "last_active": 1000.0, "turns": 1}
    # within TTL → live
    assert bridge._live_session(chat, 1000.0 + bridge.SESSION_TTL - 1) is not None
    # past TTL → evicted, returns None
    assert bridge._live_session(chat, 1000.0 + bridge.SESSION_TTL + 1) is None
    assert chat not in bridge._sessions  # evicted on expiry


def test_live_session_unknown_chat():
    bridge._sessions.clear()
    assert bridge._live_session("nobody@c.us", 123.0) is None


# ── Idempotency (duplicate webhook delivery) ─────────────────────────────────
def test_duplicate_message_id_detected_once():
    bridge._seen_msgs.clear()
    assert bridge._is_duplicate("msg-abc", 100.0) is False  # first delivery
    assert bridge._is_duplicate("msg-abc", 100.5) is True   # second delivery → skip


def test_duplicate_empty_id_never_blocks():
    bridge._seen_msgs.clear()
    assert bridge._is_duplicate("", 100.0) is False
    assert bridge._is_duplicate("", 100.0) is False


def test_duplicate_expires_after_ttl():
    bridge._seen_msgs.clear()
    bridge._is_duplicate("msg-x", 100.0)
    # after TTL the id is forgotten → treated as fresh again
    assert bridge._is_duplicate("msg-x", 100.0 + bridge._SEEN_TTL + 1) is False


# ── Output chunking ──────────────────────────────────────────────────────────
def test_short_text_is_one_chunk():
    assert bridge._split_chunks("hello world", 100) == ["hello world"]


def test_long_text_splits_within_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(50))  # well over 200
    parts = bridge._split_chunks(text, 200)
    assert len(parts) > 1
    assert all(len(p) <= 200 for p in parts)
    # reassembled content preserves every line (chunks split on newlines)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_single_overlong_line_is_hard_split():
    parts = bridge._split_chunks("y" * 1000, 300)
    assert len(parts) == 4
    assert all(len(p) <= 300 for p in parts)
    assert "".join(parts) == "y" * 1000


def test_chunk_boundary_no_split_at_limit():
    assert bridge._split_chunks("a" * 100, 100) == ["a" * 100]


# ── Session persistence (survive bridge restart) ─────────────────────────────
def test_sessions_persist_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_STATE_FILE", str(tmp_path / "s.json"))
    bridge._sessions.clear()
    bridge._sessions["me@c.us"] = {"agent": "claude", "llm": "claude",
                                   "session_id": "sid-9", "last_active": time.time(), "turns": 3}
    bridge._save_sessions()
    bridge._sessions.clear()          # simulate a restart
    bridge._load_sessions()
    assert bridge._sessions.get("me@c.us", {}).get("session_id") == "sid-9"
    assert bridge._sessions["me@c.us"]["turns"] == 3


def test_load_drops_expired_sessions(tmp_path, monkeypatch):
    f = tmp_path / "s.json"
    monkeypatch.setattr(bridge, "_STATE_FILE", str(f))
    f.write_text(json.dumps({
        "fresh@c.us": {"agent": "claude", "session_id": "f", "last_active": time.time(), "turns": 1},
        "stale@c.us": {"agent": "claude", "session_id": "s", "last_active": 0, "turns": 1},
    }))
    bridge._sessions.clear()
    bridge._load_sessions()
    assert "fresh@c.us" in bridge._sessions
    assert "stale@c.us" not in bridge._sessions  # last_active=0 is past TTL


def test_load_missing_file_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_STATE_FILE", str(tmp_path / "nope.json"))
    bridge._sessions.clear()
    bridge._sessions["keep@c.us"] = {"agent": "claude", "session_id": "k",
                                     "last_active": time.time(), "turns": 1}
    bridge._load_sessions()  # no file → leaves state untouched, no crash
    assert "keep@c.us" in bridge._sessions


# ── Artifact extraction ──────────────────────────────────────────────────────
def test_extract_artifacts_finds_existing_media(tmp_path):
    img = tmp_path / "car.png"
    img.write_bytes(b"\x89PNG fakeimage")
    assert str(img) in bridge._extract_artifacts(f"I saved the image to {img}. Done.")


def test_extract_artifacts_skips_missing_and_source(tmp_path):
    assert bridge._extract_artifacts(f"see {tmp_path}/nope.png") == []   # doesn't exist
    src = tmp_path / "bridge.py"
    src.write_text("x")
    assert bridge._extract_artifacts(f"edited {src}") == []              # source ext excluded


def test_extract_artifacts_dedupes_and_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_ARTIFACTS", 2)
    paths = []
    for i in range(4):
        p = tmp_path / f"f{i}.pdf"
        p.write_text("x")
        paths.append(str(p))
    got = bridge._extract_artifacts(" ".join(paths + [paths[0]]))  # 4 unique + 1 dup
    assert len(got) == 2  # capped at MAX_ARTIFACTS


def test_extract_artifacts_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_ARTIFACT_BYTES", 10)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * 100)
    assert bridge._extract_artifacts(f"file {big}") == []


def test_extract_artifacts_punctuation_boundaries(tmp_path):
    p = tmp_path / "report.pdf"
    p.write_bytes(b"data")
    # path wrapped in parens / followed by a comma must still resolve cleanly
    assert bridge._extract_artifacts(f"Output ({p}), enjoy") == [str(p)]


def test_human_size():
    assert bridge._human_size(512) == "512B"
    assert bridge._human_size(2048) == "2.0KB"
    assert bridge._human_size(5 * 1024 * 1024) == "5.0MB"


def test_artifacts_note_lists_paths_sizes_and_urls(tmp_path):
    p = tmp_path / "out.pdf"
    p.write_bytes(b"x" * 2048)
    bridge._artifact_tokens.clear()
    note = bridge._artifacts_note([str(p)], 1000.0)
    assert note.startswith("📎")          # prefixed so the bridge ignores its echo
    assert str(p) in note and "2.0KB" in note
    assert "/artifact/" in note and bridge.BRIDGE_PUBLIC_URL in note
    assert any(r["path"] == str(p) for r in bridge._artifact_tokens.values())


def test_register_artifact_evicts_expired():
    bridge._artifact_tokens.clear()
    bridge._artifact_tokens["old"] = {"path": "/x", "expires": 10.0}
    tok = bridge._register_artifact("/new", now=1000.0)
    assert "old" not in bridge._artifact_tokens          # expired token evicted
    assert bridge._artifact_tokens[tok]["path"] == "/new"
    assert bridge._artifact_tokens[tok]["expires"] > 1000.0


def test_serve_artifact_valid_and_invalid(tmp_path):
    import asyncio
    bridge._artifact_tokens.clear()
    p = tmp_path / "f.pdf"
    p.write_bytes(b"data")
    tok = bridge._register_artifact(str(p), now=time.time())
    assert asyncio.run(bridge.serve_artifact(tok)).status_code == 200
    assert asyncio.run(bridge.serve_artifact("bogus")).status_code == 404


def test_serve_artifact_expired_token(tmp_path):
    import asyncio
    bridge._artifact_tokens.clear()
    p = tmp_path / "f.pdf"
    p.write_bytes(b"data")
    bridge._artifact_tokens["t"] = {"path": str(p), "expires": 1.0}  # long expired
    assert asyncio.run(bridge.serve_artifact("t")).status_code == 404
