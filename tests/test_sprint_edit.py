"""Sprint files the fleet maintains: created from an approved story, one writer, burndown recorded daily."""
from __future__ import annotations

import pytest

from shared import backlog_edit as be
from shared import scrum
from shared import sprint_edit as se

D1, D2, D3 = "2026-10-31", "2026-11-01", "2026-11-02"


def _sprint(goal='Let the user pick the LLM: "carefully", with a colon'):
    t = se.new_sprint("demo", 3, goal, D1)
    t = se.add_story(t, "DM-001", "First | story", 3)
    t = se.add_story(t, "DM-002", "Second", 5)
    return se.record_burndown(t, D1)


def test_a_new_sprint_file_is_read_by_the_board_parser_with_goal_stories_and_burndown():
    s = scrum.parse_sprint(_sprint(), {}, "sprint-03.md")
    assert s["sprint"] == "3" and s["started"] == D1 and "carefully" in s["goal"] and ": " in s["goal"]
    assert [(x["id"], x["points"], x["status"]) for x in s["stories"]] == [("DM-001", 3.0, "planned"), ("DM-002", 5.0, "planned")]
    assert s["points_total"] == 8 and s["points_remaining"] == 8
    assert [(b["date"], b["remaining"]) for b in s["burndown"] if b["recorded"]] == [(D1, 8.0)]
    assert se.owner_of(_sprint()) == "dis-agent"


def test_adding_the_same_story_twice_changes_nothing_and_unknown_points_count_as_one():
    t = _sprint()
    assert se.add_story(t, "DM-001", "First", 3) == t
    assert "| DM-009 x | 1 |" in se.add_story(t, "DM-009", "x", 0)


def test_status_changes_update_the_file_and_burndown_keeps_one_row_per_day():
    t = _sprint()
    t = se.record_burndown(se.set_story_status(t, "DM-001", be.IN_PROGRESS, D2), D2)
    t = se.record_burndown(t, D2)                                                      # same day again: replaced, not duplicated
    s = scrum.parse_sprint(t, {})
    assert [(b["date"], b["remaining"]) for b in s["burndown"] if b["recorded"]] == [(D1, 8.0), (D2, 8.0)]
    assert [x["status"] for x in s["stories"]] == ["in_progress", "planned"] and "updated: " + D2 in t


def test_closing_needs_the_flag_burns_points_down_and_marks_the_sprint_complete_when_nothing_is_left():
    t = _sprint()
    with pytest.raises(se.SprintError, match="closing=True"):
        se.set_story_status(t, "DM-001", "✅ done " + D2, D2)
    t = se.record_burndown(se.set_story_status(t, "DM-001", "✅ done " + D2, D2, closing=True), D2)
    s = scrum.parse_sprint(t, {})
    assert s["points_remaining"] == 5 and [b["remaining"] for b in s["burndown"] if b["recorded"]] == [8.0, 5.0] and s["status"] == "open"
    t = se.record_burndown(se.set_story_status(t, "DM-002", "✅ done " + D3, D3, closing=True), D3)
    s = scrum.parse_sprint(t, {})
    assert s["points_remaining"] == 0 and s["status"].startswith("complete " + D3)
    with pytest.raises(se.SprintError, match="already closed"):
        se.set_story_status(t, "DM-001", be.IN_PROGRESS, D3)


def test_a_cancelled_story_leaves_the_sprint_scope():
    t = se.record_burndown(se.set_story_status(_sprint(), "DM-002", "❌ cancelled " + D2, D2, closing=True), D2)
    s = scrum.parse_sprint(t, {})
    assert s["points_total"] == 3 and s["points_remaining"] == 3 and s["stories"][1]["status"] == "cancelled"


def test_only_a_dis_agent_sprint_is_ever_edited():
    human = "---\nproject: demo\nsprint: 05\nowner: project-orchestrator\n---\n\n| Story | Points | Status |\n|---|---|---|\n| DM-001 x | 3 | 🔵 |\n"
    for fn in (lambda: se.add_story(human, "DM-002", "y", 1), lambda: se.set_story_status(human, "DM-001", be.IN_PROGRESS, D1),
               lambda: se.record_burndown(human, D1)):
        with pytest.raises(se.SprintError, match="not a dis-agent sprint"):
            fn()
    with pytest.raises(se.SprintError):
        se.add_story("no front matter\n", "DM-002", "y", 1)


def test_sprint_numbers_labels_and_the_next_free_number(tmp_path):
    (tmp_path / "demo").mkdir()
    for n in ("01", "05"):
        (tmp_path / "demo" / f"sprint-{n}.md").write_text("x")
    assert se.number_for(tmp_path, "demo", "7", {}) == 7                               # a number is used as given
    assert se.number_for(tmp_path, "demo", "S-roles", {}) == 6                          # new label: after the highest existing file
    assert se.number_for(tmp_path, "demo", "S-roles", {"S-roles": 6}) == 6              # a known label keeps its number
    assert se.number_for(tmp_path, "demo", "V2", {"S-roles": 6}) == 7                   # and the mapping counts as used
    assert se.number_for(tmp_path, "nosuch", "V1", {}) == 1


def test_a_generated_sprint_flows_through_load_and_the_per_sprint_backlog_view(tmp_path):
    from datetime import date
    from shared import scrum_digest
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "product-backlog.md").write_text(
        "| ID | Story | Priority | Status | Evidence |\n|---|---|---|---|---|\n| DM-001 | First | High | 🏃 in progress | |\n| DM-002 | Second | High | 🔵 planned | |\n| DM-003 | Later | Low | 🔵 planned | |\n")
    (tmp_path / "demo" / "sprint-03.md").write_text(_sprint())
    pr = scrum.load(tmp_path)["projects"][0]
    assert pr["sprints"][0]["points_total"] == 8 and pr["velocity"][0]["points_total"] == 8
    v = scrum_digest.backlog_view(pr, date(2026, 11, 1))
    assert [x["id"] for x in v["sprints"][0]["stories"]] == ["DM-001", "DM-002"] and [x["id"] for x in v["unscheduled"]] == ["DM-003"]
