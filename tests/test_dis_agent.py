"""dis-agent CLI pieces that do not need a live fleet."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "clients" / "fleet-mcp")]
import dis_agent as da  # noqa: E402
import scrum_cli as sc  # noqa: E402


def test_opus_is_only_reachable_through_the_capped_product_owner_role():
    assert da.MODELS["opus"] == (None, "product_owner")
    assert all(a != "claude" or r is None for (a, r) in da.MODELS.values())          # no model name maps claude+opus directly
    assert da.MODELS["auto"] == (None, None) and da.MODELS["deepseek"] == ("deepseek", None)


def test_show_names_who_answered_and_who_was_skipped():
    out = da._show({"response": "42", "answered_by": "codex",
                    "failover": [{"agent": "claude", "reason": "limit", "until": "12:50"}]})
    assert "42" in out and "— codex" in out and "claude skipped (limit, back 12:50)" in out
    assert da._show({"error": "boom"}).startswith("! boom")


def test_every_role_and_machine_the_cli_offers_exists_in_the_shipped_config():
    cfg = da.projects_cfg()
    assert set(cfg["machines"]) == {"mac-mini", "thinkpad-x1"}                         # the MacBook is never assigned work
    for name, p in cfg["projects"].items():
        assert p["repo"].startswith(("git@", "https://")) and p["dir"], name
    assert "urbanlife" in cfg["projects"] and {"product_owner", "reviewer"} <= set(sc.role_hints())


def test_chat_help_mentions_every_command_the_loop_handles():
    for cmd in ("/model", "/role", "/machine", "/project", "/new", "/who", "/quit"):
        assert cmd in da.HELP


def test_wrapper_script_is_executable_and_points_at_the_python_entry():
    w = ROOT / "scripts" / "dis-agent"
    assert w.stat().st_mode & 0o111 and "scripts/dis_agent.py" in w.read_text()
