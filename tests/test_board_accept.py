"""DI-064 stage 4: what a board confirm means, the Accept preview and its digest, the queued accept, team-story intake, and `next`."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shared import scrum_accept, scrum_flow, scrum_next, scrum_responses as sr
from shared.desk_allowlist import INTERNAL_VERBS, check_verb, is_internal

ROOT = Path(__file__).resolve().parent.parent
KEY = {"X-Secret-Key": "k"}
CFG = {"winedragons": {"repo": "https://github.com/motethansen/winedragons_asia.git"}}
PR = "https://github.com/motethansen/winedragons_asia/pull/261"


def board(evidence=f"merged {PR}", status="in_review", digest_extra=None):
    return {"digest": {"review_queue": [{"project": "winedragons", "id": "WD-373", "item_key": "winedragons:WD-373", "title": "Dish suggestions app",
                                         "evidence": evidence, **(digest_extra or {})}], "decisions": [], "adjustments": []},
            "projects": [{"project": "winedragons", "backlog_view": {"sprints": [{"sprint": 20, "stories": [{"id": "WD-373", "status": status}]}]}}]}


merged = lambda u: {"state": "MERGED", "merged_at": "2026-11-06", "title": "x"}  # noqa: E731


def not_merged(u):
    raise scrum_flow.PlanError(f"{u} is OPEN, not merged")


# ── what a confirm means ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text,kind", [
    ("The team should add the alcoholallowed gate in main() so all handlers are covered", "team"),
    ("The product owner should have the team build the level badge beside the reviewer name", "team"),
    ("The team should fix the one failing validation on VN-046", "team"),
    ("Product owner: complete one real end-to-end Google sign-up in production", "owner"),
    ("The product owner should move WD-371 out of review to done", "owner"),
    ("Michael should open the deployed build, then accept", "owner"),
    ("Product owner: look at the page for a long while and decide whether the wording reads right, and after that is settled ask the team to ship it", "owner"),
    ("", "owner"), (None, "owner"),
])
def test_classify_suggestion(text, kind):
    assert sr.classify_suggestion(text) == kind


def test_team_story_title_drops_the_lead_in_and_is_bounded():
    t = sr.team_story_title("The team should add the alcoholallowed gate in main() so five handlers are covered, and ship it as a hotfix.", "WD-327")
    assert t.startswith("WD-327: Add the alcoholallowed gate") and not t.endswith(".")
    long = sr.team_story_title("The team should " + "word " * 80)
    assert len(long) <= 141 and long.endswith("…")


# ── the preview and its digest ───────────────────────────────────────────────

def test_preview_ok_names_what_changes_and_that_nothing_is_merged():
    p = scrum_accept.accept_preview(board(), "winedragons", "WD-373", CFG, merged)
    assert p["ok"] and p["blockers"] == [] and p["prs"][0]["state"] == "MERGED"
    assert any("product-backlog.md" in w and "WD-373" in w for w in p["will_change"]) and any("sprint-20.md" in w for w in p["will_change"])
    assert "nothing is merged" in p["not_changed"] and p["digest"]


def test_preview_blocks_on_an_open_pr_and_says_merge_is_yours():
    p = scrum_accept.accept_preview(board(), "winedragons", "WD-373", CFG, not_merged)
    assert not p["ok"] and "merge it first" in p["blockers"][0] and p["prs"][0]["state"] == "OPEN"


def test_preview_blocks_without_a_pr_and_points_at_the_chat():
    p = scrum_accept.accept_preview(board(evidence="verified on a device"), "winedragons", "WD-373", CFG, merged)
    assert not p["ok"] and "--evidence" in p["blockers"][0]


def test_preview_blocks_a_story_not_in_review():
    p = scrum_accept.accept_preview(board(), "winedragons", "WD-999", CFG, merged)
    assert not p["ok"] and "not in the review queue" in p["blockers"][0] and p["digest"] is None


def test_digest_is_stable_and_changes_with_the_story_not_the_clock():
    a = scrum_accept.accept_preview(board(), "winedragons", "WD-373", CFG, merged)["digest"]
    assert a == scrum_accept.accept_preview(board(digest_extra={"as_of": "later"}), "winedragons", "WD-373", CFG, merged)["digest"]
    assert a != scrum_accept.accept_preview(board(evidence=f"merged {PR} and #262 https://github.com/motethansen/winedragons_asia/pull/262"),
                                             "winedragons", "WD-373", CFG, merged)["digest"]


# ── internal verbs are not reachable from the page ───────────────────────────

def test_internal_verbs_are_not_on_the_public_allowlist():
    for v in INTERNAL_VERBS:
        assert is_internal(v)
        with pytest.raises(ValueError):
            check_verb(v)
    assert not is_internal("plan") and not is_internal(None)


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mod(tmp_path, monkeypatch):
    from tests.test_scrum_responses import _load
    return _load(tmp_path, monkeypatch)


@pytest.fixture
def client(mod):
    return TestClient(mod.app)


def post(c, path, body, key="i1"):
    return c.post(path, json=body, headers={**KEY, **({"Idempotency-Key": key} if key else {})})


def test_page_cannot_queue_an_internal_verb(client):
    for v in INTERNAL_VERBS:
        r = post(client, "/scrum/responses", {"kind": "command", "verb": v, "project": "p", "sprint": "s1", "actor": "mike"})
        assert r.status_code == 422


def fixed_preview(mod, monkeypatch, **over):
    base = scrum_accept.accept_preview(board(), "winedragons", "WD-373", CFG, merged)
    p = {**base, **over}

    async def fake(project, story, secret):
        return p
    monkeypatch.setattr(mod, "_accept_preview", fake)
    return p


def test_accept_queues_only_with_the_digest_of_a_fresh_preview(mod, client, monkeypatch):
    p = fixed_preview(mod, monkeypatch)
    body = {"project": "winedragons", "story": "WD-373", "digest": p["digest"], "actor": "owner"}
    r = post(client, "/scrum/accept", body)
    assert r.status_code == 201 and r.json()["status"] == "queued"
    q = client.get("/scrum/desk/commands?status=queued", headers=KEY).json()
    assert q[-1]["verb"] == "accept_confirmed" and q[-1]["story_id"] == "WD-373" and PR in q[-1]["evidence"] and q[-1]["tier"] == "T2"
    # the same click again is one command
    assert post(client, "/scrum/accept", body).json()["id"] == r.json()["id"]


def test_accept_refuses_a_stale_digest_with_the_new_preview(mod, client, monkeypatch):
    fixed_preview(mod, monkeypatch)
    r = post(client, "/scrum/accept", {"project": "winedragons", "story": "WD-373", "digest": "0" * 24, "actor": "owner"}, "i2")
    assert r.status_code == 409 and r.json()["detail"]["error"] == "changed" and r.json()["detail"]["preview"]["digest"]
    assert client.get("/scrum/desk/commands?status=queued", headers=KEY).json() == []


def test_accept_refuses_a_blocked_story(mod, client, monkeypatch):
    p = fixed_preview(mod, monkeypatch, ok=False, blockers=["PR is OPEN, not merged"])
    r = post(client, "/scrum/accept", {"project": "winedragons", "story": "WD-373", "digest": p["digest"], "actor": "owner"}, "i3")
    assert r.status_code == 409 and r.json()["detail"]["error"] == "blocked"


def test_accept_needs_key_idempotency_and_valid_ids(mod, client, monkeypatch):
    p = fixed_preview(mod, monkeypatch)
    ok = {"project": "winedragons", "story": "WD-373", "digest": p["digest"], "actor": "owner"}
    assert post(client, "/scrum/accept", ok, None).status_code == 400
    assert post(client, "/scrum/accept", {**ok, "story": "bad id!"}).status_code == 422
    assert post(client, "/scrum/accept", {**ok, "extra": 1}).status_code == 422
    assert client.post("/scrum/accept", json=ok, headers={"Idempotency-Key": "x"}).status_code in (401, 403)


def respond(c, text, action="confirm", item="WD-292", key="r1"):
    return post(c, "/scrum/responses", {"item_key": f"winedragons:{item}", "project": "winedragons", "item_id": item, "section": "review",
                                         "action": action, "suggestion_id": "sg_1", "suggestion_text": text, "actor": "owner"}, key)


def test_confirm_on_a_team_suggestion_queues_a_story_once(client):
    r = respond(client, "The team should build the level badge beside the reviewer name on review cards")
    assert r.status_code == 201 and r.json()["applies"] == "queued_for_team"
    q = client.get("/scrum/desk/commands?status=queued", headers=KEY).json()
    assert [c["verb"] for c in q] == ["queue_story"] and q[0]["story_id"] == "WD-292" and q[0]["plan_digest"] == str(r.json()["id"])
    assert respond(client, "The team should build the level badge beside the reviewer name on review cards").json()["id"] == r.json()["id"]
    assert len(client.get("/scrum/desk/commands?status=queued", headers=KEY).json()) == 1


def test_confirm_on_an_owner_suggestion_only_records(client):
    r = respond(client, "Product owner: open the deployed form and confirm it works, then move it to done", item="WD-360a", key="r2")
    assert r.status_code == 201 and "applies" not in r.json()
    assert client.get("/scrum/desk/commands?status=queued", headers=KEY).json() == []


# ── the desk worker ──────────────────────────────────────────────────────────

@pytest.fixture
def dw():
    spec = importlib.util.spec_from_file_location("desk_worker_ba", ROOT / "scripts" / "desk_worker.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["desk_worker_ba"] = m
    spec.loader.exec_module(m)
    return m


class Ran:
    def __init__(self, rc=0, out="ok", err=""):
        self.calls, self.rc, self.out, self.err = [], rc, out, err

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        return type("P", (), {"returncode": self.rc, "stdout": self.out, "stderr": self.err})()


def wcfg(dw, tmp_path):
    return dw.Config("http://orch.invalid", "s", tmp_path, tmp_path / "data")


def test_worker_runs_accept_with_yes_and_the_first_pr(dw, tmp_path):
    run = Ran()
    r = dw.run_command({"id": 1, "verb": "accept_confirmed", "project": "winedragons", "sprint": "20", "story_id": "WD-373",
                        "evidence": f"{PR} https://github.com/motethansen/winedragons_asia/pull/262"}, wcfg(dw, tmp_path), run)
    a = run.calls[0]
    assert r["ok"] and a[2:4] == ["accept", "-p"] and "--story" in a and PR in a and "--yes" in a
    assert any(x.startswith("--evidence=also merged: ") and "262" in x for x in a)


def test_worker_refuses_accept_without_a_real_pr_url(dw, tmp_path):
    for ev in ("", "not-a-url", "http://evil.example/pull/1", "https://github.com/a/b/issues/3"):
        with pytest.raises(dw.CommandError) as e:
            dw.run_command({"id": 1, "verb": "accept_confirmed", "project": "winedragons", "sprint": "-", "story_id": "WD-373", "evidence": ev},
                           wcfg(dw, tmp_path), Ran())
        assert e.value.code == "no_pr"


def test_worker_reports_an_accept_that_the_cli_refused(dw, tmp_path):
    with pytest.raises(dw.CommandError) as e:
        dw.run_command({"id": 1, "verb": "accept_confirmed", "project": "winedragons", "sprint": "-", "story_id": "WD-373", "evidence": PR},
                       wcfg(dw, tmp_path), Ran(rc=1, err="not accepted: PR is OPEN"))
    assert e.value.code == "accept_failed" and "OPEN" in e.value.message


def test_worker_queue_story_passes_the_text_as_one_argument(dw, tmp_path):
    run = Ran()
    txt = "The team should add --force-everything; rm -rf / handling"
    dw.run_command({"id": 2, "verb": "queue_story", "project": "winedragons", "sprint": "-", "story_id": "WD-292", "plan_digest": "13", "evidence": txt},
                   wcfg(dw, tmp_path), run)
    a = run.calls[0]
    assert "queue-story" in a and f"--text={txt}" in a and a[a.index("--from") + 1] == "WD-292" and a[a.index("--response") + 1] == "13"


def test_worker_picks_up_internal_verbs(dw):
    assert dw.is_internal("accept_confirmed") and dw.is_internal("queue_story")
    assert not dw.check_verb_ok("accept_confirmed")      # not on the public allowlist; tick() allows it through is_internal


# ── `dis-agent next` ─────────────────────────────────────────────────────────

def row(i, key, text, action="confirm", sid="sg_1"):
    return {"id": i, "item_key": key, "action": action, "suggestion_id": sid, "suggestion_text": text, "comment": text if action == "comment" else None,
            "actor": "owner", "created_at": f"2026-11-08T01:2{i}:00Z"}


def test_pending_confirms_sort_by_meaning_and_come_first():
    b = board()
    b["digest"]["review_queue"][0]["suggestion"] = {"id": "sg_1", "text": "x"}
    b["digest"]["decisions"] = [{"item_key": "winedragons:WD-6", "project": "winedragons", "kind": "blocked", "suggestion": {"id": "sg_new", "text": "y"}}]
    b["projects"][0]["backlog_view"]["sprints"][0]["stories"].append({"id": "WD-1", "status": "done"})
    rows = [row(1, "winedragons:WD-373", "Product owner: open the app, then accept it"),
            row(2, "winedragons:WD-292", "The team should build the badge"),
            row(3, "winedragons:WD-1", "Product owner: accept it"),
            row(4, "winedragons:WD-5", "my own words", action="comment"),
            row(5, "winedragons:WD-6", "old text", sid="sg_old")]
    p = scrum_next.pending_confirms(rows, b, CFG)
    assert [e["id"] for e in p["accept"]] == ["WD-373"] and [e["id"] for e in p["team"]] == ["WD-292"]
    assert [e["item_key"] for e in p["resolved"]] == ["winedragons:WD-1"] and len(p["comments"]) == 1 and len(p["stale"]) == 1
    assert "queue-story" in p["team"][0]["command"] and "accept -p winedragons --story WD-373 --pr " + PR in p["accept"][0]["command"]
    text = scrum_next.render_pending(p, [])
    assert text.startswith("WAITING ON YOU FIRST") and "does not accept a story" in text and "already closed" in text
    assert scrum_next.render_pending({k: [] for k in ("accept", "team", "other", "comments", "stale", "resolved")}, []) == ""


# ── dis-agent queue-story ────────────────────────────────────────────────────

BACKLOG = """# Demo

## Open items

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| DM-001 | **First** | High | 🔵 planned | notes |

## Closed items

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| DM-010 | Old | High | ✅ done 2026-08-01 | PR #1 |
"""


def test_queue_story_adds_one_planned_row_and_is_idempotent_per_response(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "scripts"))
    import dis_agent as da
    repo = tmp_path / "viz"
    (repo / "demo").mkdir(parents=True)
    (repo / "demo" / "product-backlog.md").write_text(BACKLOG)
    for c in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"], ["add", "-A"], ["commit", "-q", "-m", "i"]):
        subprocess.run(["git", "-C", str(repo), *c], check=True, capture_output=True)
    monkeypatch.setenv("VIZNEO_REPO", str(repo))
    monkeypatch.setattr(da, "_report", lambda *a, **k: None)
    args = SimpleNamespace(project="demo", text="The team should add the gate in main() and ship it as a hotfix.", source="DM-001", response=7)
    da.cmd_queue_story(args)
    text = (repo / "demo" / "product-backlog.md").read_text()
    assert "DM-011" in text and "DM-001: Add the gate in main()" in text and "🔵 planned" in text and "(response #7)" in text
    da.cmd_queue_story(args)                              # the same confirm again adds nothing
    assert (repo / "demo" / "product-backlog.md").read_text().count("DM-0") == text.count("DM-0")
    with pytest.raises(SystemExit):
        da.cmd_queue_story(SimpleNamespace(project="demo", text="  ", source=None, response=None))
