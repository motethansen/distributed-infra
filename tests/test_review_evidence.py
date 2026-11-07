"""The whole review must reach the person (and the desk), a stated verdict must gate closing, and a review of an
older state must be called stale."""
from shared import scrum_flow as sf


def task(i, role, status="done", text="", created="2026-11-06T09:00:00", agent="codex"):
    return {"id": i * 8, "status": status, "agent": agent, "created_at": created,
            "payload": {"project": "p", "sprint": "2", "story": "S-2", "role": role},
            "result": {"answer": text, "text": text}}


LONG = "**Review incomplete.**\n\n" + "\n".join(f"{n}. finding number {n}, with detail " + "x" * 220 for n in range(1, 6))


def test_the_whole_review_is_returned_not_an_excerpt(monkeypatch):
    monkeypatch.setattr(sf, "answer", lambda t: (t.get("result") or {}).get("text", ""))
    ev = sf.story_evidence([task("a", "reviewer", text=LONG)], "p", "2", "S-2")
    assert ev["review_full"] == LONG and len(ev["review_full"]) > 700
    assert "finding number 5" in ev["review_full"] and "finding number 5" not in ev["review_excerpt"]


def test_verdict_line_wins_then_old_wording_then_unknown():
    assert sf.parse_review_verdict("x\nVERDICT: request changes") == "REQUEST_CHANGES"
    assert sf.parse_review_verdict("**Verdict: APPROVE**") == "APPROVE"
    assert sf.parse_review_verdict("a\nVERDICT: INCOMPLETE\nVERDICT: APPROVE") == "APPROVE"       # the last line wins
    assert sf.parse_review_verdict("Review incomplete — cannot approve S-2") == "INCOMPLETE"
    assert sf.parse_review_verdict("The implementation does not fully meet the acceptance criteria.") == "REQUEST_CHANGES"
    assert sf.parse_review_verdict("looks fine") is None and sf.parse_review_verdict("") is None


def test_review_prompt_asks_for_a_verdict_and_forbids_approving_from_a_summary():
    p = sf.review_prompt({"id": "S-1", "title": "t", "acceptance": ["a"]}, "claude", "material")
    assert "VERDICT: INCOMPLETE" in p and "Do not approve from the author's own summary" in p


def test_work_after_the_review_makes_it_stale(monkeypatch):
    monkeypatch.setattr(sf, "answer", lambda t: (t.get("result") or {}).get("text", ""))
    rows = [task("r", "reviewer", text="VERDICT: APPROVE", created="2026-11-06T09:35:00"),
            task("d", "dev_frontend", created="2026-11-06T10:00:00")]
    assert sf.story_evidence(rows, "p", "2", "S-2")["review_stale"] is True
    rows[1]["created_at"] = "2026-11-06T09:00:00"
    assert sf.story_evidence(rows, "p", "2", "S-2")["review_stale"] is False


def test_newest_finished_review_is_the_one_shown(monkeypatch):
    monkeypatch.setattr(sf, "answer", lambda t: (t.get("result") or {}).get("text", ""))
    rows = [task("o", "reviewer", text="Review incomplete", created="2026-11-06T09:35:00"),
            task("n", "reviewer", text="VERDICT: REQUEST_CHANGES", created="2026-11-07T12:00:00")]
    ev = sf.story_evidence(rows, "p", "2", "S-2")
    assert ev["review_verdict"] == "REQUEST_CHANGES" and ev["review_task"].startswith("n")


def test_review_prompt_fills_in_the_acceptance_criteria_and_the_material():
    """A literal `{json.dumps(...)}` once reached the reviewer because an f-string prefix was lost."""
    p = sf.review_prompt({"id": "S-9", "title": "T", "acceptance": ["first criterion", "second criterion"]}, "claude",
                         "WHERE-THE-CODE-IS", {"passed": True, "counts": {"pass": 2, "fail": 0, "unverified": 0}})
    assert "first criterion" in p and "second criterion" in p
    assert "WHERE-THE-CODE-IS" in p and "{json" not in p and "{material" not in p and "{validation" not in p
    assert "S-9" in p and "{story" not in p
