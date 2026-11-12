"""DI-082: n-agents is registered in the desk, kept off DeepSeek, and the backlog functions work for it."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "clients" / "fleet-mcp")]
import dis_agent as da  # noqa: E402

from shared import scrum_flow as sf  # noqa: E402

TEMPLATE = """---
project: {name}
---
# {name} backlog

## Vision

Production version of the distributed agents.

## Open items

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| NA-001 | first story | High | 🔵 planned | |
| NA-002 | second story | Medium | 🔵 planned | |
"""


def _yaml(name):
    return yaml.safe_load((ROOT / "config" / name).read_text())


def test_n_agents_is_a_known_project_with_repo_and_dir():
    p = _yaml("scrum_projects.yaml")["projects"]["n-agents"]
    assert p["repo"] == "git@github.com:motethansen/n-agents.git"
    assert p["dir"] == "n-agents"
    assert "Vision" in p["note"] and "never edit ~/Projects/distributed-infra" in p["note"]


def test_n_agents_is_not_deepseek_and_not_isolated():
    pol = _yaml("roles.yaml")
    assert "n-agents" not in pol["deepseek_projects"]
    assert "n-agents" not in (pol.get("isolated_projects") or [])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("VIZNEO_REPO", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-only")
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(k, v)
    (tmp_path / "n-agents").mkdir()
    (tmp_path / "n-agents" / "product-backlog.md").write_text(TEMPLATE.format(name="n-agents"), encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(tmp_path), *cmd], check=True, capture_output=True)
    return tmp_path


def test_register_story_adds_a_planned_row(repo):
    r = sf.register_story(repo, "n-agents", {"title": "Token dashboard"}, "tester", "2026-11-12", "plan-x")
    assert r["id"] == "NA-003" and r["committed"] is True
    text = (repo / "n-agents" / "product-backlog.md").read_text(encoding="utf-8")
    assert "NA-003" in text and "Token dashboard" in text


def test_queue_story_is_idempotent_per_response(repo, monkeypatch):
    monkeypatch.setattr(da, "_report", lambda what, r: None)
    a = SimpleNamespace(project="n-agents", text="The team should add a usage page", source=None, response=7)
    da.cmd_queue_story(a)
    da.cmd_queue_story(a)
    text = (repo / "n-agents" / "product-backlog.md").read_text(encoding="utf-8")
    assert text.count("(response #7)") == 1 and "NA-003" in text and "NA-004" not in text


def test_status_listing_sees_the_n_agents_backlog(repo):
    from shared import scrum
    projects = scrum.load(repo)["projects"]
    p = next(x for x in projects if x["project"] == "n-agents")
    assert p["stories"] == 2
