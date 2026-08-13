"""Tests for assistant_run's resolution of the ai_agent_assistant checkout.

Regression cover: the module-level default used to be a literal
"/Users/YOUR_USERNAME/Projects/github/ai_agent_assistant" placeholder. No machine
resolves that, and neither worker sets ASSISTANT_PROJECT_DIR, so every `assist`
command from WhatsApp failed with "Assistant venv not found at
/Users/YOUR_USERNAME/...".
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

MODULE = "worker.handlers.assistant_run"


def _reload(monkeypatch, env: str | None, home: str | None = None):
    """Re-import the handler so its module-level path constants re-evaluate."""
    if env is None:
        monkeypatch.delenv("ASSISTANT_PROJECT_DIR", raising=False)
    else:
        monkeypatch.setenv("ASSISTANT_PROJECT_DIR", env)
    if home is not None:
        monkeypatch.setenv("HOME", home)
    mod = importlib.import_module(MODULE)
    return importlib.reload(mod)


def test_default_contains_no_placeholder(monkeypatch, tmp_path):
    """The default must never be an unexpanded template path."""
    mod = _reload(monkeypatch, env=None, home=str(tmp_path))

    assert "YOUR_USERNAME" not in str(mod.ASSISTANT_DIR), (
        f"placeholder leaked into the default path: {mod.ASSISTANT_DIR}")


def test_default_resolves_under_the_real_home(monkeypatch, tmp_path):
    """Unset env → ~/Projects/github/ai_agent_assistant, the fleet convention."""
    mod = _reload(monkeypatch, env=None, home=str(tmp_path))

    assert mod.ASSISTANT_DIR == tmp_path / "Projects" / "github" / "ai_agent_assistant"
    assert mod.ASSISTANT_PYTHON == mod.ASSISTANT_DIR / "venv" / "bin" / "python"


def test_env_override_wins(monkeypatch, tmp_path):
    """An explicit checkout location still takes precedence."""
    custom = tmp_path / "elsewhere" / "assistant"
    mod = _reload(monkeypatch, env=str(custom), home=str(tmp_path))

    assert mod.ASSISTANT_DIR == custom


def test_empty_env_falls_back_rather_than_resolving_to_cwd(monkeypatch, tmp_path):
    """ASSISTANT_PROJECT_DIR="" must not collapse to Path("") (the cwd)."""
    mod = _reload(monkeypatch, env="", home=str(tmp_path))

    assert mod.ASSISTANT_DIR == tmp_path / "Projects" / "github" / "ai_agent_assistant"
    assert str(mod.ASSISTANT_DIR) not in (".", ""), "empty env collapsed to cwd"


@pytest.fixture(autouse=True)
def _restore_module(monkeypatch):
    """Leave the module in its pristine state for other test files."""
    yield
    monkeypatch.undo()
    importlib.reload(importlib.import_module(MODULE))
