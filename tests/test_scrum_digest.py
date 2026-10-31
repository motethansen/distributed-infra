"""The scrum-of-scrums digest: review queue, decisions, stalls, adjustments."""
from __future__ import annotations

from datetime import date

from shared import scrum, scrum_digest as sd

TODAY = date(2026, 10, 31)


def proj(name, counts=None, items=(), curve_last=None, sprints=(), velocity=(), closed=3, stories=10):
    c = {"done": 0, "superseded": 0, "in_progress": 0, "in_review": 0, "blocked": 0, "planned": 0}
    c.update(counts or {})
    return {"project": name, "counts": c, "closed": closed, "stories": stories, "complete_pct": 30.0, "active_items": list(items),
            "scurve": {"curve": [{"date": curve_last}] if curve_last else []}, "sprints": list(sprints), "velocity": list(velocity)}


def item(i, st, ev="e"):
    return {"id": i, "status": st, "title": f"title {i}", "evidence": ev}


def sprint(n, total, done, started="2026-10-30", drift=()):
    return {"sprint": n, "points_total": total, "points_done": done, "points_remaining": total - done, "started": started,
            "updated": started, "drift": list(drift)}


def test_in_review_stories_form_the_review_queue_and_blocked_ones_need_a_decision():
    d = sd.build([proj("a", {"in_review": 1, "blocked": 1}, [item("A-1", "in_review", "PR 3"), item("A-2", "blocked"), item("A-3", "in_progress")])], TODAY)
    assert [r["id"] for r in d["review_queue"]] == ["A-1"] and d["review_queue"][0]["evidence"] == "PR 3"
    assert [x["kind"] for x in d["decisions"]] == ["blocked"] and "A-2" in d["decisions"][0]["text"]
    assert d["adjustments"][0].startswith("1 stories are waiting for your sprint review")


def test_a_project_with_open_work_and_no_recent_closure_is_stalled_only_at_the_threshold():
    open_ = {"planned": 4}
    for last, stalled in (("2026-10-17", True), ("2026-10-18", False)):         # 14 days vs 13 days
        d = sd.build([proj("a", open_, curve_last=last)], TODAY)
        assert any(x["kind"] == "stalled" for x in d["decisions"]) is stalled, last
    d = sd.build([proj("a", {}, curve_last="2026-01-01")], TODAY)                 # nothing open: not stalled
    assert d["decisions"] == []
    d = sd.build([proj("a", open_)], TODAY)                                       # never closed anything dated: no claim made
    assert d["decisions"] == []


def test_sprint_adjustments_cover_zero_progress_old_sprints_and_drift():
    d = sd.build([proj("a", sprints=[sprint(1, 8, 0)]), proj("b", sprints=[sprint(2, 10, 4, started="2026-10-20")]),
                  proj("c", sprints=[sprint(3, 10, 5, drift=["C-1", "C-2"])])], TODAY)
    txt = " | ".join(d["adjustments"])
    assert "a sprint 1: 0 of 8 points done" in txt and "b sprint 2: started 11 days ago with 6 of 10" in txt
    assert "c sprint 3: 2 stories where the sprint file and the backlog disagree" in txt
    assert "b sprint 2" in txt and "started 1 days ago" not in txt


def test_velocity_is_the_mean_of_the_last_three_sprints_and_projects_sort_review_first():
    v = [{"points_total": 5, "points_done": p} for p in (1, 10, 20, 30)] + [{"points_total": 0, "points_done": 99}]
    d = sd.build([proj("quiet", velocity=v), proj("zbusy", {"in_review": 2, "in_progress": 1})], TODAY)
    assert [r["project"] for r in d["projects"]] == ["zbusy", "quiet"]
    assert d["projects"][1]["velocity_avg"] == 20.0


def test_opus_budget_warning_starts_at_eighty_percent():
    assert not any("Opus" in a for a in sd.build([], TODAY, {"spent": 39.99, "cap": 50})["adjustments"])
    assert any("Opus budget is 80% used" in a for a in sd.build([], TODAY, {"spent": 40.0, "cap": 50})["adjustments"])


def test_markdown_lists_every_section_and_truncates_long_evidence():
    d = sd.build([proj("a", {"in_review": 1}, [item("A-1", "in_review", "x" * 500)])], TODAY)
    md = sd.to_markdown(d, fleet="1,000 tasks")
    assert "## Ready for your review (1)" in md and "## Needs a decision (0)" in md and "- nothing" in md
    assert "1,000 tasks" in md and "| a | 3/10 |" in md and "x" * 240 in md and "x" * 241 not in md


def test_active_items_are_read_from_a_real_backlog_table_without_emoji_in_titles():
    bl = ("| ID | Story | Priority | Status | Evidence |\n|---|---|---|---|---|\n"
          "| AA-1 | 🔍 **Fix the thing** | High | 🔍 in review | PR #4 shipped |\n"
          "| AA-2 | Blocked thing | Low | 🚧 blocked | waiting on Stripe |\n"
          "| AA-3 | Planned | Low | 🔵 planned | |\n| AA-4 | Done | Low | ✅ done 2026-01-01 | |\n")
    got = scrum.active_items(bl)
    assert [(g["id"], g["status"], g["title"], g["evidence"]) for g in got] == [
        ("AA-1", "in_review", "Fix the thing", "PR #4 shipped"), ("AA-2", "blocked", "Blocked thing", "waiting on Stripe")]


# ── backlog_view: the product backlog per sprint ─────────────────────────────

def _view_project():
    items = [item2("V-1", "in_review", "PR 4"), item2("V-2", "blocked", "waiting on Stripe"), item2("V-3", "planned"),
             item2("V-4", "in_progress", "running"), item2("V-5", "cancelled", "not needed", "2026-10-20"),
             item2("V-6", "cancelled", "ancient", "2026-06-01"), item2("V-7", "cancelled", "undated"), item2("V-8", "planned", "just a note")]
    sprints = [{"sprint": "2", "goal": "g2", "started": "2026-10-25", "status": "open", "points_total": 8, "points_done": 3, "points_remaining": 5,
                "stories": [{"id": "V-1", "title": "one", "status": "in_review", "points": 3, "assignee": "me"},
                            {"id": "V-9", "title": "closed one", "status": "done", "points": 3, "assignee": None},
                            {"id": "V-8", "title": "planned one", "status": "planned", "points": 1, "assignee": None},
                            {"id": "V-6", "title": "ancient cancelled", "status": "cancelled", "points": 2, "assignee": None}]},
               {"sprint": "10", "goal": "g10", "started": "2026-10-30", "status": "open", "points_total": 2, "points_done": 0, "points_remaining": 2,
                "stories": [{"id": "V-2", "title": "two", "status": "blocked", "points": 2, "assignee": None}]},
               {"sprint": "1", "goal": "g1", "stories": []}]
    return {"project": "v", "closed": 4, "counts": {}, "backlog_items": items, "sprints": sprints}


def item2(i, st, ev="", date_=None):
    return {"id": i, "status": st, "title": f"title {i}", "evidence": ev, "priority": "High", "date": date_}


def test_sprints_are_listed_newest_first_with_reasons_on_blocked_and_in_flight_stories_only():
    v = sd.backlog_view(_view_project(), TODAY)
    assert [s["sprint"] for s in v["sprints"]] == ["10", "2", "1"]                   # numeric order, not "10" < "2"
    s10 = v["sprints"][0]["stories"][0]
    assert s10["id"] == "V-2" and s10["reason"] == "waiting on Stripe"
    s2 = {x["id"]: x for x in v["sprints"][1]["stories"]}
    assert s2["V-1"]["reason"] == "PR 4" and s2["V-9"]["reason"] == "" and s2["V-8"]["reason"] == ""   # only in-flight/blocked/cancelled carry one


def test_unscheduled_holds_only_stories_in_no_sprint_ordered_by_how_alive_they_are():
    v = sd.backlog_view(_view_project(), TODAY)
    assert [i["id"] for i in v["unscheduled"]] == ["V-4", "V-3"]                   # V-1 and V-2 are in sprints; cancelled are separate
    assert v["closed_unscheduled"] == 3                                             # 4 closed overall, 1 of them (V-9) is in a sprint


def test_cancelled_stories_show_for_sixty_days_then_hide_and_undated_ones_stay_visible():
    v = sd.backlog_view(_view_project(), TODAY)
    assert [c["id"] for c in v["cancelled"]] == ["V-5", "V-7"]
    assert v["cancelled_hidden"] == 1                                              # V-6 (in a sprint, 152 days old) is hidden
    assert all(s["id"] != "V-6" for sp in v["sprints"] for s in sp["stories"])
    edge = {"project": "x", "closed": 0, "backlog_items": [item2("E-1", "cancelled", "", "2026-09-01"), item2("E-2", "cancelled", "", "2026-09-02")]}
    vv = sd.backlog_view(edge, TODAY)                                               # 60 days vs 59 days
    assert [c["id"] for c in vv["cancelled"]] == ["E-1", "E-2"] and vv["cancelled_hidden"] == 0
    edge["backlog_items"][0]["date"] = "2026-08-31"                                 # 61 days
    vv = sd.backlog_view(edge, TODAY)
    assert [c["id"] for c in vv["cancelled"]] == ["E-2"] and vv["cancelled_hidden"] == 1


def test_blocked_decisions_carry_the_reason_or_say_none_was_recorded():
    p = proj("a", {"blocked": 2}, [item("A-1", "blocked", "needs a Stripe key from the dashboard"), item("A-2", "blocked", "")])
    d = {x["text"].split(" is ")[0]: x for x in sd.build([p], TODAY)["decisions"] if x["kind"] == "blocked"}
    assert d["A-1"]["reason"] == "needs a Stripe key from the dashboard" and "— needs a Stripe key" in d["A-1"]["text"]
    assert "no reason recorded in the backlog" in d["A-2"]["text"] and d["A-2"]["reason"] == ""
