"""DI-082 acceptance (independent): n-agents is registered, never routed to DeepSeek, and the backlog
helpers can take a first story on an empty backlog."""
from __future__ import annotations

from pathlib import Path

import yaml

from agents import roles
from shared import backlog_edit as be

ROOT = Path(__file__).resolve().parent.parent
EMPTY = """---
project: n-agents
---
# n-agents backlog

## Open items

<!-- id-prefix: NA -->

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
"""


def _load(name):
    return yaml.safe_load((ROOT / "config" / name).read_text())


def test_registry_entry_is_exact():
    projects = _load("scrum_projects.yaml")["projects"]
    e = projects["n-agents"]
    assert e["repo"] == "git@github.com:motethansen/n-agents.git"
    assert e["dir"] == "n-agents"
    assert len(projects) >= 10  # other entries still there
    assert "budgetapp" in projects and "winedragons" in projects


def test_not_in_deepseek_projects():
    pol = _load("roles.yaml")
    assert "n-agents" not in [str(x).lower() for x in pol["deepseek_projects"]]


def test_backend_dev_falls_back_off_deepseek():
    pol = _load("roles.yaml")
    r = roles.resolve("dev_backend", project="n-agents", policy=pol)
    assert r["agent"] != "deepseek"


def test_first_story_on_empty_backlog():
    assert be.prefix_of(EMPTY) == "NA"
    new, sid = be.add_story(EMPTY, "Landing page", "High")
    assert sid == "NA-001"
    assert "Landing page" in new
    new2, sid2 = be.add_story(new, "Second")
    assert sid2 == "NA-002"
    assert be.status_of(new2, "NA-001")


def test_empty_backlog_without_marker_still_errors():
    import pytest
    with pytest.raises(be.BacklogError):
        be.prefix_of(EMPTY.replace("<!-- id-prefix: NA -->", ""))
