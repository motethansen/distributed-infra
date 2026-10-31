"""dis-agent next: ranked suggestions with the exact command, from the board and the local plans."""
from __future__ import annotations

from shared import scrum_next as sn


def proj(name, unscheduled=(), sprints=(), cancelled=()):
    return {"project": name, "backlog_view": {"sprints": list(sprints), "unscheduled": list(unscheduled), "cancelled": list(cancelled)}}


def st(i, status, evidence=""):
    return {"id": i, "status": status, "evidence": evidence}


def board(projects, review=(), decisions=(), opus=None):
    return {"projects": projects, "digest": {"review_queue": list(review), "decisions": list(decisions), "opus": opus}}


def plan(p="demo", bid="DM-1", approved=True, ran=True, sprint=1):
    return {"project": p, "sprint": "S", "sprint_number": sprint, "story_id": "S-1", "backlog_id": bid, "title": "Pick the LLM",
            "approved": approved, "ran": ran, "path": f"data/scrum/{p}/S/S-1.plan.json"}


def titles(sugs):
    return [x["title"] for x in sugs]


def test_a_finished_fleet_story_is_first_and_points_at_close_with_a_preview():
    b = board([proj("demo", sprints=[{"points_remaining": 3, "stories": [st("DM-1", "in_review")]}])], review=[{"project": "demo", "id": "DM-1"}])
    s = sn.suggest(b, [plan()], {})
    assert s[0]["priority"] == 1 and "/close demo 1 DM-1" in s[0]["command"] and "--check" in s[0]["command"] and s[0]["kind"] == sn.READ
    assert not any("marked in review" in t for t in titles(s))                       # a story with a plan is not also listed as unplanned review


def test_closed_cancelled_or_vanished_stories_produce_no_suggestion():
    for status in ("done", "superseded", "cancelled"):
        b = board([proj("demo", unscheduled=[st("DM-1", status)])])
        assert sn.suggest(b, [plan()], {}) == []
    assert sn.suggest(board([proj("demo")]), [plan()], {}) == []                    # not in the backlog view at all
    closed = board([proj("demo", unscheduled=[st("DM-1", "done")])])
    assert sn.suggest(closed, [plan(approved=False, ran=False)], {}) == []          # an unapproved plan for a closed story is not a task


def test_an_approved_plan_that_has_not_run_says_go_and_an_unapproved_one_says_approve():
    b = board([proj("demo", unscheduled=[st("DM-1", "planned")])])
    go = sn.suggest(b, [plan(approved=True, ran=False)], {})
    assert go[0]["command"] == "dis-agent go data/scrum/demo/S/S-1.plan.json" and go[0]["kind"] == sn.RUNS
    ap = sn.suggest(b, [plan(approved=False, ran=False)], {})
    assert ap[0]["command"].startswith("dis-agent approve") and ap[0]["kind"] == sn.CHANGES and "voids an approval" in ap[0]["why"]
    running = sn.suggest(board([proj("demo", unscheduled=[st("DM-1", "in_progress")])]), [plan(approved=True, ran=True)], {})
    assert running == []                                                             # approved and already ran: nothing for you yet


def test_a_project_with_open_work_and_no_sprint_gets_sprint_when_groomed_and_groom_when_not():
    open_ = [st(f"D-{i}", "planned") for i in range(4)]
    s = sn.suggest(board([proj("a", open_), proj("b", open_[:2])]), [], {"a": "2026-10-31"})
    assert s[0]["command"] == "/sprint a" and "groomed 2026-10-31" in s[0]["title"] and "Opus" in s[0]["cost"]
    assert any(x["command"] == "/groom b" for x in s)
    # only the biggest ungroomed project is suggested overall; with -p it is that project
    many = board([proj("x", open_), proj("y", open_[:3]), proj("z", open_[:1])])
    assert [x["command"] for x in sn.suggest(many, [], {}) if x["command"].startswith("/groom")] == ["/groom x"]
    assert [x["command"] for x in sn.suggest(many, [], {}, project="z")] == ["/groom z"]


def test_no_sprint_suggestion_when_a_sprint_is_running_or_a_plan_exists_or_nothing_is_open():
    running = proj("a", [st("A-9", "planned")], sprints=[{"points_remaining": 2, "stories": [st("A-1", "in_progress")]}])
    assert sn.suggest(board([running]), [], {}) == []
    assert sn.suggest(board([proj("b", [st("B-1", "planned")])]), [plan("b", "B-0", approved=True, ran=True)], {}) == []
    assert sn.suggest(board([proj("c")]), [], {}) == []
    done_sprint = proj("d", [st("D-1", "planned")], sprints=[{"points_remaining": 0, "stories": [st("D-0", "done")]}])
    assert sn.suggest(board([done_sprint]), [], {"d": "x"})[0]["command"] == "/sprint d"        # a finished sprint is not a running one


def test_unplanned_review_stories_are_one_line_overall_and_per_project_when_asked():
    rq = [{"project": "a", "id": "A-1"}, {"project": "a", "id": "A-2"}, {"project": "b", "id": "B-1"}]
    b = board([proj("a"), proj("b")], review=rq)
    s = sn.suggest(b, [], {})
    assert titles(s) == ["3 stories are marked in review across 2 projects (a, b)"] and s[0]["command"] == "/review"
    one = sn.suggest(b, [], {}, project="b")
    assert titles(one) == ["b: 1 story is marked in review (B-1)"] and one[0]["command"] == "/backlog b"


def test_blocked_stories_with_no_reason_and_stalled_projects_and_the_opus_budget_are_flagged():
    b = board([proj("a", [st("A-1", "blocked", ""), st("A-2", "blocked", "needs Stripe"), st("A-3", "blocked", "")])],
              decisions=[{"kind": "stalled", "project": "a", "text": "a: nothing closed for 20 days"}, {"kind": "stalled", "project": "z", "text": "z"},
                         {"kind": "blocked", "project": "a", "text": "ignored"}], opus={"spent": 41.0, "cap": 50.0})
    s = {x["title"]: x for x in sn.suggest(b, [], {})}
    assert "a: 2 blocked stories have no reason recorded (A-1, A-3)" in s
    assert "2 projects look stalled (a, z)" in s and "Opus budget is 82% used ($41.00 of $50)" in s
    assert sn.suggest(board([], opus={"spent": 39.0, "cap": 50.0}), [], {}) == []              # under 80%: quiet
    assert titles(sn.suggest(board([], opus={"spent": 40.0, "cap": 50.0}), [], {})) == ["Opus budget is 80% used ($40.00 of $50)"]   # exactly 80% warns
    only_a = sn.suggest(b, [], {}, project="a")
    assert "a: nothing closed for 20 days" in titles(only_a) and not any("Opus" in t for t in titles(only_a))


def test_order_limit_dedupe_and_render():
    open_ = [st("D-1", "planned")]
    b = board([proj("a", open_), proj("b", open_)], decisions=[{"kind": "stalled", "project": "a", "text": "t"}])
    s = sn.suggest(b, [plan("a", "A-1", approved=False, ran=False)], {"b": "2026-10-31"}, limit=2)
    assert len(s) == 2 and [x["priority"] for x in s] == sorted(x["priority"] for x in s)
    text = sn.render(s)
    assert text.startswith("1. ") and "command: " in text and "effect:  " in text and "why:     " in text
    assert sn.render([]) == "Nothing needs you right now.\n"


def test_suggestions_are_sorted_by_priority_whatever_the_order_they_were_found_in_and_the_limit_applies():
    b = board([proj("a", unscheduled=[st("A-1", "planned")], sprints=[{"points_remaining": 1, "stories": [st("A-2", "in_review")]}]),
               proj("b", [st("B-1", "planned")]), proj("c", [st("C-1", "planned")])],
              decisions=[{"kind": "stalled", "project": "a", "text": "t"}])
    plans = [plan("a", "A-1", approved=False, ran=False), plan("a", "A-2", approved=True, ran=True)]
    b["projects"][0]["backlog_view"]["unscheduled"].append(st("A-2", "in_review"))
    full = sn.suggest(b, plans, {"b": "d"}, limit=0)
    assert len(full) >= 4 and [x["priority"] for x in full] == sorted(x["priority"] for x in full) and full[0]["priority"] == 1
    assert len(sn.suggest(b, plans, {"b": "d"}, limit=2)) == 2 and sn.suggest(b, plans, {"b": "d"}, limit=2) == full[:2]


def test_review_items_carry_sprint_prs_and_the_right_confirming_command():
    b = board([proj("p", sprints=[{"sprint": "2", "stories": [st("P-1", "in_review")]}])],
              review=[{"project": "p", "id": "P-1", "title": "t1", "evidence": "PR #7 green"},
                      {"project": "p", "id": "P-2", "title": "t2", "evidence": "checked by hand"},
                      {"project": "p", "id": "P-3", "title": "t3", "evidence": ""},
                      {"project": "q", "id": "Q-1", "title": "t4", "evidence": "PR #1"}])
    cfg = {"p": {"repo": "git@github.com:o/p.git"}}
    items = {i["id"]: i for i in sn.review_items(b, [plan("p", "P-3", sprint=5)], cfg)}
    assert items["P-1"]["sprint"] == "2" and items["P-1"]["prs"] == ["https://github.com/o/p/pull/7"] and not items["P-1"]["fleet"]
    assert items["P-1"]["command"] == "dis-agent accept -p p --story P-1 --pr https://github.com/o/p/pull/7"
    assert items["P-2"]["sprint"] is None and 'accept -p p --story P-2 --evidence "what you verified"' in items["P-2"]["command"]
    assert items["P-3"]["fleet"] is True and items["P-3"]["command"] == "/close p 5 P-3"                    # a fleet plan means /close, not accept
    assert items["Q-1"]["prs"] == [] and "--evidence" in items["Q-1"]["command"]                           # no repo configured: no guessed link
    assert [i["id"] for i in sn.review_items(b, [], cfg, project="p")] == ["P-1", "P-2", "P-3"]


def test_a_waiting_proposal_is_first_and_replaces_the_start_a_sprint_or_groom_suggestions():
    open_ = [st("D-1", "planned")]
    props = {"a": {"sprint": "1", "stories": 8, "points": 16, "todo": ["S-1", "S-3"], "planned": 0}}
    s = sn.suggest(board([proj("a", open_), proj("b", open_)]), [], {"a": "2026-10-31"}, proposals=props)
    assert s[0]["priority"] == 1 and "A sprint proposal for a is waiting for you: sprint 1, 8 stories, 16 points, 2 not planned yet (S-1, S-3)" == s[0]["title"]
    assert '--revise "<your feedback>"' in s[0]["command"] and "plan -p a -s 1 --story <ID>" in s[0]["command"]
    assert not any(x["command"] == "/sprint a" or x["command"] == "/groom a" for x in s)                  # no second sprint for the same project
    assert any(x["command"] == "/groom b" for x in s)                                                    # other projects are unaffected
    only_b = sn.suggest(board([proj("a", open_), proj("b", open_)]), [], {}, project="b", proposals=props)
    assert not any("proposal" in x["title"] for x in only_b)                                             # -p filters proposals too
