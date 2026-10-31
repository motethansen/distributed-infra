"""Scrum flow: parsing/validating model output, dependency waves, and the dispatcher against a fake queue."""
from __future__ import annotations

import json

import httpx
import pytest

from shared import scrum_flow as sf

ROLES = {"product_owner", "scrum_master", "analyst", "dev_frontend", "dev_backend", "integrator", "reviewer"}
STORY = {"id": "BA-201", "title": "Gift money", "acceptance": ["POST /gift returns 201"]}


# ── parsing ──────────────────────────────────────────────────────────────────

def test_extract_json_tolerates_fences_and_prose():
    assert sf.extract_json('Sure!\n```json\n{"a": [1, 2]}\n```\nbye') == {"a": [1, 2]}
    assert sf.extract_json("noise {not json} then [1,2]") == [1, 2]
    for bad in ("", "   ", "no json here"):
        with pytest.raises(sf.PlanError):
            sf.extract_json(bad)


def test_parse_stories_needs_title_and_acceptance_and_assigns_ids():
    s = sf.parse_stories('{"stories":[{"title":"A","acceptance":["x"]},{"id":"Z-9","title":"B","acceptance":["y"]}]}')
    assert [x["id"] for x in s] == ["S-1", "Z-9"]
    for bad in ('{"stories":[]}', '{"stories":[{"acceptance":["x"]}]}', '{"stories":[{"title":"A","acceptance":[]}]}',
                '{"stories":[{"title":"A"}]}'):
        with pytest.raises(sf.PlanError):
            sf.parse_stories(bad)


def _plan(*tasks):
    return json.dumps({"tasks": list(tasks)})


def _t(key, role="dev_backend", deps=(), prompt="do it"):
    return {"key": key, "role": role, "depends_on": list(deps), "prompt": prompt}


def test_parse_plan_accepts_a_valid_plan_and_orders_it_in_waves():
    tasks = sf.parse_plan(_plan(_t("api"), _t("ui", "dev_frontend", ["api"]), _t("doc", "analyst")), ROLES)
    layers = sf.waves(tasks)
    assert [[t["key"] for t in l] for l in layers] == [["api", "doc"], ["ui"]]


@pytest.mark.parametrize("plan,msg", [
    (_plan(_t("a", "ceo")), "unknown role"),
    (_plan(_t("a", "reviewer")), "not assigned by the Scrum Master"),
    (_plan(_t("a", "integrator")), "not assigned by the Scrum Master"),
    (_plan(_t("a"), _t("a")), "repeated"),
    (_plan(_t("a", deps=["ghost"])), "unknown task"),
    (_plan(_t("a", deps=["a"])), "itself"),
    (_plan(_t("a", deps=["b"]), _t("b", deps=["a"])), "cycle"),
    (_plan(_t("a", prompt="  ")), "empty prompt"),
    (_plan(*[_t(f"t{i}") for i in range(sf.MAX_TASKS + 1)]), "limit"),
    ('{"tasks": []}', "non-empty"),
])
def test_parse_plan_refuses_what_it_cannot_run(plan, msg):
    with pytest.raises(sf.PlanError, match=msg):
        sf.parse_plan(plan, ROLES)


# ── fake queue ───────────────────────────────────────────────────────────────

class Queue:
    """POST /tasks completes the task at once. `agent_for(payload)` decides who 'ran' it."""

    def __init__(self, agent_for=None, fail_keys=(), down=()):
        self.tasks, self.order, self.down, self.events = {}, [], set(down), []
        self.agent_for = agent_for or (lambda p: {"dev_backend": "deepseek", "reviewer": "codex"}.get(p.get("role"), "claude"))
        self.fail_keys = set(fail_keys)

    def handler(self, req: httpx.Request) -> httpx.Response:
        base = f"{req.url.host}:{req.url.port}"
        if base in self.down:
            return httpx.Response(503)
        if req.headers.get("x-secret-key") != "k":
            return httpx.Response(401, json={"detail": "bad"})
        if req.method == "POST" and req.url.path == "/tasks":
            body = json.loads(req.content)
            p = body["payload"]
            tid = f"id{len(self.tasks) + 1:03d}xxxx"
            agent = self.agent_for(p)
            bad = p.get("flow_key") in self.fail_keys
            self.tasks[tid] = {"id": tid, "payload": p, "status": "needs_human" if bad else "done", "agent": agent,
                               "notes": "boom" if bad else None,
                               "result": {"ok": not bad, "agent": agent, "response": self.reply(p)}}
            self.order.append(tid)
            self.events.append(("POST", p.get("flow_key") or p["role"]))
            return httpx.Response(201, json={"id": tid})
        if req.method == "GET" and req.url.path.startswith("/tasks/"):
            t = self.tasks[req.url.path.rsplit("/", 1)[1]]
            self.events.append(("GET", t["payload"].get("flow_key") or t["payload"]["role"]))
            return httpx.Response(200, json=t)
        return httpx.Response(404)

    def reply(self, p):
        if p.get("role") == "tester":
            n = p["prompt"].count("\n1. ") and 1
            return 'ran it.\n{"validation":{"suite":"3 passed, 0 failed","criteria":[{"n":1,"status":"pass","evidence":"test_x"}]}}'
        return f"output of {p.get('flow_key') or p['role']}"

    def client(self, urls=("http://a:1",)):
        return sf.FleetClient(list(urls), "k", transport=httpx.MockTransport(self.handler))

    def payloads(self):
        return [self.tasks[i]["payload"] for i in self.order]


def _run(q, tasks, **kw):
    log = []
    out = sf.dispatch(q.client(), "budgetapp", "S6", STORY, sf.parse_plan(_plan(*tasks), ROLES),
                      go=kw.pop("go", True), sleep=lambda s: None, say=log.append, **kw)
    return out, log


# ── dispatcher ───────────────────────────────────────────────────────────────

def test_dry_run_queues_nothing():
    q = Queue()
    out, log = _run(q, [_t("api")], go=False)
    assert out == [] and q.order == [] and any("dry run" in l for l in log)


def test_every_queued_task_carries_project_sprint_story_and_role():
    q = Queue()
    _run(q, [_t("api"), _t("ui", "dev_frontend", ["api"])])
    assert q.payloads()
    for p in q.payloads():
        assert (p["project"], p["sprint"], p["story"]) == ("budgetapp", "S6", "BA-201") and p["role"]


def test_dependency_results_are_handed_to_the_next_task_and_waves_run_in_order():
    q = Queue(agent_for=lambda p: "claude")
    _run(q, [_t("api", "dev_frontend"), _t("ui", "dev_frontend", ["api"], prompt="build ui")])
    ui = next(p for p in q.payloads() if p.get("flow_key") == "ui")
    assert "output of api" in ui["prompt"] and ui["prompt"].startswith("build ui")
    keys = [p.get("flow_key") for p in q.payloads() if p.get("flow_key")]
    assert keys.index("api") < keys.index("ui")






def test_reviewer_is_pinned_off_codex_when_codex_was_the_author_and_a_same_family_review_is_flagged():
    q = Queue(agent_for=lambda p: "codex")
    out, log = _run(q, [_t("ui", "dev_frontend")])
    assert q.payloads()[-1].get("agent") == "claude"
    q2 = Queue(agent_for=lambda p: "claude")                  # reviewer fell back to the author's family
    out2, log2 = _run(q2, [_t("ui", "dev_frontend")])
    assert out2[-1]["same_family"] is True and any("SAME model family" in l for l in log2)


def test_analyst_tasks_are_not_reviewed():
    q = Queue(agent_for=lambda p: "agy")
    _run(q, [_t("doc", "analyst")])
    assert [p["role"] for p in q.payloads()] == ["analyst"]


def test_a_failed_task_stops_the_run_before_its_dependants_start():
    q = Queue(fail_keys={"api"})
    with pytest.raises(sf.PlanError, match="api ended needs_human"):
        _run(q, [_t("api"), _t("ui", "dev_frontend", ["api"])])
    assert all(p.get("flow_key") != "ui" for p in q.payloads())


def test_approval_and_cwd_reach_every_task():
    q = Queue()
    _run(q, [_t("api")], approved=True, cwd="~/Projects/x")
    assert all(p.get("approved") is True and p.get("cwd") == "~/Projects/x" for p in q.payloads())
    q2 = Queue()
    _run(q2, [_t("api")])
    assert all("approved" not in p for p in q2.payloads())


# ── PO / SM steps and the client ─────────────────────────────────────────────

def test_po_and_sm_steps_queue_tagged_tasks_and_parse_the_reply():
    stories = {"stories": [{"id": "BA-201", "title": "Gift", "acceptance": ["201"]}]}
    q = Queue(agent_for=lambda p: "claude")
    q.agent_for = lambda p: "claude"
    c = q.client()
    orig = q.handler

    def handler(req):
        r = orig(req)
        if req.method == "POST":
            tid = q.order[-1]
            role = q.tasks[tid]["payload"]["role"]
            q.tasks[tid]["result"]["response"] = json.dumps(stories if role == "product_owner" else {"tasks": [_t("api", "dev_frontend")]})
        return r

    c = sf.FleetClient(["http://a:1"], "k", transport=httpx.MockTransport(handler))
    got, _ = sf.propose_stories(c, "budgetapp", "S6", "gift money", as_agent="agy", sleep=lambda s: None)
    assert got[0]["id"] == "BA-201"
    po = q.payloads()[0]
    assert (po["role"], po["project"], po["sprint"], po["agent"]) == ("product_owner", "budgetapp", "S6", "agy")
    tasks, _ = sf.plan_story(c, "budgetapp", "S6", got[0], {"dev_frontend": "ui", "reviewer": "r"}, sleep=lambda s: None)
    assert tasks[0]["key"] == "api" and q.payloads()[1]["role"] == "scrum_master" and q.payloads()[1]["story"] == "BA-201"
    assert "reviewer" not in q.payloads()[1]["prompt"].split("Roles you may assign:")[1].split("Rules:")[0]   # not offered


def test_client_fails_over_on_5xx_and_raises_on_4xx():
    q = Queue(down={"a:1"})
    t = q.client(("http://a:1", "http://b:2")).queue({"role": "analyst", "prompt": "x"}, "n")
    assert t["id"] in q.tasks
    with pytest.raises(sf.PlanError, match="no orchestrator reachable"):
        Queue(down={"a:1"}).client().queue({"role": "x"}, "n")
    bad = sf.FleetClient(["http://a:1"], "wrong", transport=httpx.MockTransport(Queue().handler))
    with pytest.raises(sf.PlanError, match="HTTP 401"):
        bad.queue({}, "n")


def test_wait_times_out_instead_of_hanging():
    c = sf.FleetClient(["http://a:1"], "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"id": "i", "status": "in_progress"})))
    with pytest.raises(sf.PlanError, match="timed out"):
        c.wait(["i"], timeout=0, sleep=lambda s: None)


def test_dev_backend_that_fell_back_to_claude_wrote_its_own_files_so_no_integrator():
    q = Queue(agent_for=lambda p: "claude" if p["role"] != "reviewer" else "codex")
    _run(q, [_t("api", "dev_backend")])
    assert [p["role"] for p in q.payloads()] == ["dev_backend", "tester", "reviewer"]


# ── machines, repo preamble, approval, review ────────────────────────────────

MACHINES = {"mac-mini", "thinkpad-x1"}


def test_plan_machine_must_be_a_known_one_and_required_needs_a_machine():
    ok = sf.parse_plan(_plan({**_t("a"), "machine": "thinkpad-x1", "machine_required": True}), ROLES, MACHINES)
    assert ok[0]["machine"] == "thinkpad-x1" and ok[0]["machine_required"] is True
    with pytest.raises(sf.PlanError, match="machine 'macbook-pro'"):
        sf.parse_plan(_plan({**_t("a"), "machine": "macbook-pro"}), ROLES, MACHINES)
    no_machine = sf.parse_plan(_plan({**_t("a"), "machine_required": True}), ROLES, MACHINES)
    assert no_machine[0]["machine"] is None and no_machine[0]["machine_required"] is False


def test_machine_choice_reaches_the_queue_soft_or_hard():
    q = Queue(agent_for=lambda p: "claude")
    _run(q, [{**_t("a", "dev_frontend"), "machine": "mac-mini"},
             {**_t("b", "dev_frontend"), "machine": "thinkpad-x1", "machine_required": True}])
    a = next(p for p in q.payloads() if p.get("flow_key") == "a")
    b = next(p for p in q.payloads() if p.get("flow_key") == "b")
    assert a["_preferred_machine"] == "mac-mini" and "_target_machine" not in a
    assert b["_target_machine"] == "thinkpad-x1" and "_preferred_machine" not in b


CFG = {"repo": "git@github.com:x/y.git", "dir": "y", "note": "careful"}




def test_approval_binds_to_the_exact_tasks_and_editing_voids_it(tmp_path):
    f = tmp_path / "p.plan.json"
    f.write_text(json.dumps({"project": "x", "sprint": "S", "story": STORY, "tasks": [_t("a")]}))
    assert sf.is_approved(json.loads(f.read_text())) is False
    plan = sf.approve_plan(f, "michael", "2026-10-30T12:00:00+00:00")
    assert sf.is_approved(plan) and sf.is_approved(json.loads(f.read_text()))
    edited = json.loads(f.read_text())
    edited["tasks"][0]["prompt"] = "something else"
    assert sf.is_approved(edited) is False
    unapproved_by = json.loads(f.read_text())
    unapproved_by["approval"]["by"] = ""
    assert sf.is_approved(unapproved_by) is False


def test_sprint_review_counts_only_this_sprints_tasks_and_surfaces_prs_and_stuck_work():
    def t(i, sprint="S6", project="budgetapp", status="done", role="dev_frontend", agent="claude", out=10, resp=""):
        return {"id": f"{i:08d}xx", "status": status, "role": role, "agent": agent, "assigned_to": "mac-mini",
                "tokens_in": 100, "tokens_out": out, "cost_usd": 0.5, "notes": "stuck here" if status != "done" else None,
                "payload": {"project": project, "sprint": sprint}, "result": {"response": resp}}
    rows = [t(1, resp="Opened https://github.com/o/r/pull/7 done"), t(2, role="reviewer", agent="codex", out=5),
            t(3, status="needs_human"), t(4, sprint="S5"), t(5, project="other")]
    data, md = sf.sprint_review(rows, "budgetapp", "S6", {"points_done": 3, "points_total": 8, "points_remaining": 5})
    assert data["tasks"] == 3 and data["tokens_out"] == 25 and data["cost_usd"] == 1.5
    assert data["by_role"] == {"dev_frontend": 2, "reviewer": 1} and data["by_status"] == {"done": 2, "needs_human": 1}
    assert data["pull_requests"] == ["https://github.com/o/r/pull/7"]
    assert data["needs_attention"][0]["notes"] == "stuck here"
    assert "3 of 8 points done" in md and "pull/7" in md and "stuck here" in md


def test_analyst_tasks_get_no_repo_instructions():
    q = Queue(agent_for=lambda p: "agy")
    _run(q, [_t("doc", "analyst")], project_cfg=CFG)
    assert q.payloads()[0]["prompt"] == "do it"


# ── one branch per story, batched integration, serialized editors, one review ─

def test_all_deepseek_code_of_a_wave_is_applied_by_ONE_integrator_then_the_story_is_reviewed_once():
    q = Queue()
    out, _ = _run(q, [_t("a", "dev_backend"), _t("b", "dev_backend"), _t("c", "dev_frontend", ["a", "b"])])
    roles = [p["role"] for p in q.payloads()]
    assert roles.count("integrator") == 1 and roles.count("reviewer") == 1 and roles[-1] == "reviewer"
    integ = next(p for p in q.payloads() if p["role"] == "integrator")
    assert "task a" in integ["prompt"] and "task b" in integ["prompt"] and "output of a" in integ["prompt"] and "output of b" in integ["prompt"]
    assert roles.index("integrator") < roles.index("dev_frontend")        # later tasks see the code on the branch
    assert [e["key"] for e in out if e["role"] == "reviewer"] == ["review"]


def test_everything_works_on_one_story_branch_and_reviews_are_not_told_where_the_code_is():
    q = Queue(agent_for=lambda p: "claude" if p["role"] != "reviewer" else "codex")
    _run(q, [_t("ui", "dev_frontend"), _t("ui2", "dev_frontend", ["ui"])], project_cfg=CFG)
    by = {p.get("flow_key") or p["role"]: p["prompt"] for p in q.payloads()}
    for k in ("ui", "ui2"):
        assert "git@github.com:x/y.git" in by[k] and "`dis/BA-201`" in by[k] and "ONE branch" in by[k] and "careful" in by[k]
        assert "never merge" in by[k].lower()
    assert "git@github.com:x/y.git" not in by["review"]
    q2 = Queue()
    _run(q2, [_t("api", "dev_backend")])                                  # no project config -> prompt untouched
    assert q2.payloads()[0]["prompt"] == "do it"


def test_file_editing_tasks_run_one_at_a_time_but_text_tasks_run_together():
    q = Queue(agent_for=lambda p: "claude")
    _run(q, [_t("e1", "dev_frontend"), _t("e2", "dev_frontend")])
    ev = [e for e in q.events if e[1] in ("e1", "e2")]
    assert ev == [("POST", "e1"), ("GET", "e1"), ("POST", "e2"), ("GET", "e2")]      # e2 queued only after e1 finished
    q2 = Queue()
    _run(q2, [_t("d1", "dev_backend"), _t("d2", "dev_backend")])
    ev2 = [e for e in q2.events if e[1] in ("d1", "d2")]
    assert [e[0] for e in ev2[:2]] == ["POST", "POST"]                                # both queued before either is polled


def test_the_story_review_is_pinned_to_claude_when_codex_wrote_any_of_it_and_flags_same_family():
    q = Queue(agent_for=lambda p: "codex" if p["role"] == "dev_frontend" else "claude")
    out, _ = _run(q, [_t("ui", "dev_frontend")])
    assert q.payloads()[-1]["role"] == "reviewer" and q.payloads()[-1].get("agent") == "claude"
    assert out[-1]["same_family"] is False and out[-1]["authors"] == ["codex"]
    q2 = Queue(agent_for=lambda p: "claude")
    out2, log2 = _run(q2, [_t("ui", "dev_frontend")])
    assert out2[-1]["same_family"] is True and any("SAME model family" in l for l in log2)


def test_a_claude_dev_task_needs_no_integrator_and_an_analyst_only_story_is_not_reviewed():
    q = Queue(agent_for=lambda p: "claude" if p["role"] != "reviewer" else "codex")
    _run(q, [_t("ui", "dev_frontend")])
    assert [p["role"] for p in q.payloads()] == ["dev_frontend", "tester", "reviewer"]
    q2 = Queue(agent_for=lambda p: "agy")
    _run(q2, [_t("doc", "analyst")])
    assert [p["role"] for p in q2.payloads()] == ["analyst"]


def test_an_analyst_is_not_an_author_of_the_code_under_review():
    q = Queue(agent_for=lambda p: {"dev_frontend": "claude"}.get(p["role"], "codex"))
    out, _ = _run(q, [_t("ui", "dev_frontend"), _t("doc", "analyst")])
    assert out[-1]["authors"] == ["claude"] and out[-1]["same_family"] is False
    assert q.payloads()[-1].get("agent") is None


# ── backlog sync (git) ───────────────────────────────────────────────────────

BACKLOG = """# Demo

## Open items

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| DM-001 | First | High | 🔵 planned | |
"""


def _git(cwd, *a):
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture
def vrepo(tmp_path, monkeypatch):
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t" if "NAME" in k else "t@t")
    import subprocess
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    (work / "demo").mkdir()
    (work / "demo" / "product-backlog.md").write_text(BACKLOG)
    _git(work, "add", "."); _git(work, "commit", "-qm", "init"); _git(work, "push", "-q", "origin", "HEAD:main")
    return work, origin


def test_register_then_progress_then_review_then_close_only_with_the_confirmed_path(vrepo):
    work, origin = vrepo
    story = {"id": "S-1", "title": "Pick the LLM from any client"}
    r = sf.register_story(work, "demo", story, "michael", "2026-10-31", "S-1.plan.json")
    assert r["id"] == "DM-002" and r["committed"] and r["pushed"]
    assert "story registered as planned" in _git(origin, "log", "-1", "--format=%s", "main")
    sf.mark_story(work, "demo", "DM-002", "🏃 in progress", None)
    with pytest.raises(sf.PlanError, match="not in review"):
        sf.close_story(work, "demo", "DM-002", "michael", "2026-10-31", "PR 1")          # still in progress: cannot close
    sf.mark_story(work, "demo", "DM-002", "🔍 in review", "PR https://github.com/o/r/pull/9")
    sf.close_story(work, "demo", "DM-002", "michael", "2026-10-31", "PR https://github.com/o/r/pull/9")
    text = (work / "demo" / "product-backlog.md").read_text()
    row = next(l for l in text.split("\n") if l.startswith("| DM-002"))
    assert "✅ done 2026-10-31" in row and "confirmed by michael" in row and "pull/9" in row
    assert "closed after sprint review, confirmed by michael" in _git(origin, "log", "-1", "--format=%s", "main")
    from shared import scrum
    assert scrum.parse_backlog(text) == {"DM-001": "planned", "DM-002": "done"}


def test_edits_refuse_to_run_over_the_humans_uncommitted_changes_and_report_a_failed_push(vrepo):
    work, origin = vrepo
    f = work / "demo" / "product-backlog.md"
    f.write_text(f.read_text() + "\nhuman typing...\n")
    with pytest.raises(sf.PlanError, match="uncommitted changes"):
        sf.mark_story(work, "demo", "DM-001", "🏃 in progress", None)
    assert "human typing" in f.read_text() and "in progress" not in f.read_text()          # untouched
    _git(work, "checkout", "--", "."); _git(work, "remote", "remove", "origin")
    r = sf.mark_story(work, "demo", "DM-001", "🏃 in progress", None)
    assert r["committed"] is True and r["pushed"] is False and r["push_error"]
    with pytest.raises(sf.PlanError, match="not in the backlog"):
        sf.mark_story(work, "demo", "DM-099", "🏃 in progress", None)
    with pytest.raises(sf.PlanError, match="not found"):
        sf.mark_story(work, "nosuch", "X-1", "🏃 in progress", None)


def test_story_evidence_is_scoped_to_the_story_and_ignores_planning_tasks():
    def t(i, story="S-1", role="dev_frontend", status="done", resp="", created="2026-10-31T10:0%d:00" % 0, sprint="S", project="p"):
        return {"id": f"{i:08d}x", "status": status, "agent": "claude", "created_at": created,
                "payload": {"project": project, "sprint": sprint, "story": story, "role": role}, "result": {"response": resp}}
    rows = [t(1, resp="PR https://github.com/o/r/pull/5"), t(2, role="reviewer", resp="LGTM, one nit", created="2026-10-31T10:09:00"),
            t(3, role="reviewer", resp="older review", created="2026-10-31T10:01:00"),
            t(4, story="S-2", resp="https://github.com/o/r/pull/6"), t(5, role="scrum_master", resp="https://github.com/o/r/pull/7"),
            t(6, status="needs_human"), t(7, sprint="other")]
    ev = sf.story_evidence(rows, "p", "S", "S-1")
    assert ev["tasks"] == 4 and ev["done"] == 3 and ev["stuck"] == ["00000006"] and ev["pull_requests"] == ["https://github.com/o/r/pull/5"]
    assert ev["reviewed"] is True and ev["review_excerpt"] == "LGTM, one nit"
    assert sf.story_evidence(rows, "p", "S", "S-9")["reviewed"] is False


# ── independent validation ───────────────────────────────────────────────────

def _val(*statuses, suite="9 passed, 0 failed"):
    crit = [{"n": i, "status": s, "evidence": f"e{i}"} for i, s in enumerate(statuses, 1)]
    return "Done.\n" + json.dumps({"validation": {"suite": suite, "criteria": crit}})


def test_validation_passes_only_when_every_criterion_passes_and_none_are_missing():
    ok = sf.parse_validation(_val("pass", "pass"), 2)
    assert ok["passed"] is True and ok["counts"] == {"pass": 2, "fail": 0, "unverified": 0} and ok["suite"] == "9 passed, 0 failed"
    assert sf.parse_validation(_val("pass", "fail"), 2)["passed"] is False
    assert sf.parse_validation(_val("pass", "unverified"), 2)["passed"] is False              # unverified needs a human
    short = sf.parse_validation(_val("pass"), 3)                                              # the tester skipped criteria
    assert short["passed"] is False and short["complete"] is False
    assert sf.parse_validation(_val("pass", "bogus"), 2)["criteria"][1]["status"] == "unverified"


def test_the_model_cannot_declare_its_own_pass_and_garbage_is_not_a_verdict():
    lie = 'x {"validation":{"passed":true,"criteria":[{"n":1,"status":"fail","evidence":"boom"}]}}'
    assert sf.parse_validation(lie, 1)["passed"] is False
    for bad in ("", "no json", '{"validation": "ok"}', '{"validation":{"criteria":[]}}'):
        v = sf.parse_validation(bad, 1)
        assert v["passed"] is None and v["error"]
    last = sf.parse_validation(_val("fail") + "\n" + _val("pass"), 1)                         # the last block wins
    assert last["passed"] is True


def test_the_tester_runs_after_the_dev_work_and_before_the_review_and_the_reviewer_is_told_the_verdict():
    q = Queue(agent_for=lambda p: "claude" if p["role"] != "reviewer" else "codex")
    out, log = _run(q, [_t("ui", "dev_frontend")])
    assert [p["role"] for p in q.payloads()] == ["dev_frontend", "tester", "reviewer"]
    tester = next(p for p in q.payloads() if p["role"] == "tester")
    assert "ACCEPTANCE CRITERIA" in tester["prompt"] and "POST /gift returns 201" in tester["prompt"] and "`dis/BA-201`" in tester["prompt"]
    assert "1 pass, 0 fail, 0 unverified" in q.payloads()[-1]["prompt"]
    assert out[-2]["role"] == "tester" and out[-2]["validation"]["passed"] is True
    assert any("validation: 1 pass" in l for l in log)


def test_a_failed_tester_task_does_not_stop_the_review_but_leaves_no_verdict():
    q = Queue(agent_for=lambda p: "claude" if p["role"] != "reviewer" else "codex", fail_keys={"validate"})
    out, _ = _run(q, [_t("ui", "dev_frontend")])
    v = next(e for e in out if e["role"] == "tester")["validation"]
    assert v["passed"] is None and "tester task failed" in v["error"]
    assert q.payloads()[-1]["role"] == "reviewer" and "the tester task failed" in q.payloads()[-1]["prompt"]


def test_story_evidence_carries_the_latest_validation():
    base = {"project": "p", "sprint": "S", "story": "S-1"}
    rows = [{"id": "00000001x", "status": "done", "agent": "claude", "created_at": "1", "payload": {**base, "role": "dev_frontend"}, "result": {}},
            {"id": "00000002x", "status": "done", "agent": "agy", "created_at": "2", "payload": {**base, "role": "tester"},
             "result": {"response": _val("pass", "fail")}}]
    ev = sf.story_evidence(rows, "p", "S", "S-1")
    assert ev["validation"]["passed"] is False and ev["validation"]["counts"]["fail"] == 1
    assert sf.story_evidence(rows[:1], "p", "S", "S-1")["validation"] is None
    rows[1]["status"] = "needs_human"
    assert sf.story_evidence(rows, "p", "S", "S-1")["validation"]["passed"] is None


# ── Product Owner: architecture alignment, briefs, feedback rounds, grooming ──

def test_po_prompt_asks_for_alignment_briefs_and_carries_feedback_only_on_a_revision():
    first = sf.po_prompt("p", "S", "gift money", context="CTX-MARK")
    assert "CTX-MARK" in first and "architecture" in first and "BRIEF" in first and '"briefs"' in first and "concerns" in first
    assert "feedback at sprint planning" not in first
    rev = sf.po_prompt("p", "S", "gift money", previous=[{"id": "S-1", "title": "old"}], feedback="drop the currency part")
    assert "drop the currency part" in rev and '"old"' in rev and "feedback at sprint planning" in rev


def test_context_has_the_estate_architecture_in_full_other_notes_by_title_and_only_open_rows(tmp_path):
    arch = tmp_path / "_architecture"
    arch.mkdir()
    (arch / "estate-architecture.md").write_text("ESTATE RULES " + "x" * 100)
    (arch / "demo-design.md").write_text("DEMO DESIGN NOTE")
    (arch / "unrelated.md").write_text("SHOULD NOT APPEAR IN FULL")
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "product-backlog.md").write_text(BACKLOG + "| DM-005 | Done thing | High | ✅ done 2026-01-01 | |\n")
    c = sf.load_context(tmp_path, "demo")
    assert "ESTATE RULES" in c and "DEMO DESIGN NOTE" in c and "SHOULD NOT APPEAR" not in c
    assert "Other architecture notes (titles only): unrelated.md" in c
    assert "DM-001" in c and "DM-005" not in c and "1 open rows" in c
    (tmp_path / "demo" / "product-backlog.md").write_text(
        "| ID | Story | Priority | Status | Evidence |\n|---|---|---|---|---|\n| DM-001 | x | High | 🚧 blocked on DM-000 | waiting for the login flow |\n"
        "| DM-002 | gone | Low | ❌ cancelled 2026-10-01 | dropped |\n")
    c2 = sf.load_context(tmp_path, "demo")
    assert "blocked on DM-000 — waiting for the login flow" in c2 and "| High |" in c2 and "DM-002" not in c2     # reasons are sent, cancelled are not
    assert len(sf.load_context(tmp_path, "demo", max_chars=50)) == 50                     # capped


def _po_queue(stories_json):
    q = Queue(agent_for=lambda p: "claude")
    q.reply = lambda p: stories_json
    return q


def test_propose_stories_returns_concerns_and_sends_the_previous_round_and_feedback():
    body = json.dumps({"concerns": ["conflicts with DM-001"], "stories": [
        {"id": "S-1", "title": "Gift", "acceptance": ["201"], "briefs": {"dev_backend": "build x"}}]})
    q = _po_queue(body)
    stories, done = sf.propose_stories(q.client(), "demo", "S", "gift", "CTX", previous=[{"id": "S-0"}], feedback="smaller")
    assert stories[0]["briefs"] == {"dev_backend": "build x"} and done["concerns"] == ["conflicts with DM-001"]
    sent = q.payloads()[0]
    assert sent["role"] == "product_owner" and "smaller" in sent["prompt"] and "CTX" in sent["prompt"] and '"S-0"' in sent["prompt"]


def test_groom_returns_the_review_and_refuses_a_reply_without_one():
    ok = json.dumps({"summary": "fine", "review": [{"id": "DM-001", "verdict": "ok"}], "missing": []})
    rep, done = sf.groom_backlog(_po_queue(ok).client(), "demo", "ctx")
    assert rep["review"][0]["verdict"] == "ok"
    with pytest.raises(sf.PlanError, match="no review list"):
        sf.groom_backlog(_po_queue('{"summary":"x"}').client(), "demo", "ctx")
    q = _po_queue(ok)
    sf.groom_backlog(q.client(), "demo", "ctx")
    assert q.payloads()[0]["role"] == "product_owner" and q.payloads()[0]["sprint"] == "grooming" and "OPEN stories" in q.payloads()[0]["prompt"]


def test_cancel_marks_the_story_with_the_date_and_reason_and_cannot_be_undone_or_reasonless(vrepo):
    work, origin = vrepo
    with pytest.raises(sf.PlanError, match="needs a reason"):
        sf.cancel_story(work, "demo", "DM-001", "michael", "2026-10-31", "  ")
    r = sf.cancel_story(work, "demo", "DM-001", "michael", "2026-10-31", "superseded by the new design")
    assert r["committed"] and r["pushed"]
    row = next(l for l in (work / "demo" / "product-backlog.md").read_text().split("\n") if l.startswith("| DM-001"))
    assert "❌ cancelled 2026-10-31" in row and "cancelled by michael: superseded by the new design" in row
    from shared import scrum
    assert scrum.parse_backlog((work / "demo" / "product-backlog.md").read_text())["DM-001"] == "cancelled"
    assert "cancelled by michael" in _git(origin, "log", "-1", "--format=%s", "main")
    with pytest.raises(sf.PlanError, match="already closed"):
        sf.mark_story(work, "demo", "DM-001", "🏃 in progress", None)                     # a cancelled story is not quietly revived


# ── sprint file sync (git) ───────────────────────────────────────────────────

def test_the_sprint_file_is_created_on_the_first_story_then_follows_the_story_and_closes_only_when_told(vrepo):
    work, origin = vrepo
    day = "2026-10-31"
    story = {"id": "S-1", "title": "Pick the LLM", "points": 3}
    r = sf.sprint_open_story(work, "demo", 4, story, "DM-002", "Let users pick the model", day)
    assert r["committed"] and r["pushed"]
    f = work / "demo" / "sprint-04.md"
    from shared import scrum
    assert "DM-002 Pick the LLM" in f.read_text() and scrum.parse_sprint(f.read_text(), {})["points_remaining"] == 3
    assert "sprint 04 — DM-002 planned" in _git(origin, "log", "-1", "--format=%s", "main")
    sf.sprint_open_story(work, "demo", 4, {"id": "S-2", "title": "Second", "points": 2}, "DM-003", "ignored for an existing file", day)
    assert scrum.parse_sprint(f.read_text(), {})["points_total"] == 5 and "ignored for an existing file" not in f.read_text()
    sf.sprint_mark(work, "demo", 4, "DM-002", "🏃 in progress", day)
    sf.sprint_mark(work, "demo", 4, "DM-002", "✅ done 2026-11-01", "2026-11-01", closing=True)
    s = scrum.parse_sprint(f.read_text(), {})
    assert s["points_remaining"] == 2 and [b["remaining"] for b in s["burndown"] if b["recorded"]] == [5.0, 2.0]
    assert sf.find_sprint_of(work, "demo", "DM-003") == 4 and sf.find_sprint_of(work, "demo", "DM-999") is None
    with pytest.raises(Exception, match="closing=True"):
        sf.sprint_mark(work, "demo", 4, "DM-003", "✅ done 2026-11-01", "2026-11-01")          # not without the closing flag


def test_a_hand_written_sprint_file_is_refused_and_so_are_uncommitted_edits(vrepo):
    work, origin = vrepo
    f = work / "demo" / "sprint-05.md"
    f.write_text("---\nproject: demo\nsprint: 05\nowner: project-orchestrator\n---\n\n## Stories\n\n| Story | Points | Status |\n|---|---|---|\n")
    _git(work, "add", "."); _git(work, "commit", "-qm", "human sprint"); _git(work, "push", "-q", "origin", "HEAD:main")
    before = f.read_text()
    with pytest.raises(sf.PlanError, match="not a dis-agent sprint"):
        sf.sprint_open_story(work, "demo", 5, {"id": "S-1", "title": "x", "points": 1}, "DM-009", "g", "2026-10-31")
    assert f.read_text() == before and _git(work, "status", "--porcelain").strip() == ""      # untouched, nothing staged
    f.write_text(before + "| DM-777 mentioned in a human sprint | 1 | 🔵 |\n")
    _git(work, "add", "."); _git(work, "commit", "-qm", "mention"); _git(work, "push", "-q", "origin", "HEAD:main")
    assert sf.find_sprint_of(work, "demo", "DM-777") is None                                    # only dis-agent sprints are ever located
    g = work / "demo" / "sprint-06.md"
    sf.sprint_open_story(work, "demo", 6, {"id": "S-1", "title": "x", "points": 1}, "DM-009", "g", "2026-10-31")
    g.write_text(g.read_text() + "\nhuman typing\n")
    with pytest.raises(sf.PlanError, match="uncommitted changes"):
        sf.sprint_mark(work, "demo", 6, "DM-009", "🏃 in progress", "2026-11-01")


def test_validate_prompt_for_a_merged_story_tests_that_branch_and_never_pushes_to_it():
    st = {"id": "S-1", "title": "T", "acceptance": ["one"]}
    own = sf.validate_prompt(st)
    assert "Work on the story branch `dis/S-1`" in own and "already merged" not in own
    merged = sf.validate_prompt(st, "main")
    assert "check out `main`" in merged and "Do NOT push to `main`" in merged and "dis/S-1-validation" in merged and "open a pull request" in merged
    assert "Work on the story branch" not in merged and "ACCEPTANCE CRITERIA" in merged and "1. one" in merged


# ── stories the fleet did not build ──────────────────────────────────────────

def test_repo_slug_and_pr_links_resolve_the_prs_a_backlog_row_mentions():
    assert sf.repo_slug("git@github.com:motethansen/budgetapp.git") == "motethansen/budgetapp"
    assert sf.repo_slug("https://github.com/o/n") == "o/n" and sf.repo_slug("git@gitlab.com:o/n.git") is None and sf.repo_slug(None) is None
    slug = "o/p"
    assert sf.pr_links("PR #108, CI green", slug) == ["https://github.com/o/p/pull/108"]
    assert sf.pr_links("PRs #12, #13 and #14 merged", slug) == [f"https://github.com/o/p/pull/{n}" for n in (12, 13, 14)]
    assert sf.pr_links("PR #109 (stacked on #108)", slug) == ["https://github.com/o/p/pull/109"]          # #108 is a mention, not a PR ref
    assert sf.pr_links("see pull/7 and https://github.com/x/y/pull/9", slug) == ["https://github.com/x/y/pull/9", "https://github.com/o/p/pull/7"]
    assert sf.pr_links("issue #5, no PR", slug) == [] and sf.pr_links("PR #4", None) == []                  # no repo, no guessing
    assert sf.pr_links("PR #4 and PR #4", slug) == ["https://github.com/o/p/pull/4"]                         # de-duplicated


def test_verify_pr_merged_trusts_only_github_saying_merged():
    import types

    def gh(stdout="", rc=0, err=""):
        return lambda *a, **k: types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=err)
    ok = sf.verify_pr_merged("u", run=gh(json.dumps({"state": "MERGED", "mergedAt": "2026-10-30T10:00:00Z", "title": "Ledger"})))
    assert ok == {"merged_at": "2026-10-30", "title": "Ledger"}
    with pytest.raises(sf.PlanError, match="is OPEN, not merged"):
        sf.verify_pr_merged("u", run=gh(json.dumps({"state": "OPEN", "mergedAt": None, "title": "x"})))
    with pytest.raises(sf.PlanError, match="is CLOSED, not merged"):
        sf.verify_pr_merged("u", run=gh(json.dumps({"state": "CLOSED", "mergedAt": None, "title": "x"})))
    with pytest.raises(sf.PlanError, match="could not ask GitHub"):
        sf.verify_pr_merged("u", run=gh(rc=1, err="HTTP 404"))

    def boom(*a, **k):
        raise FileNotFoundError("gh")
    with pytest.raises(sf.PlanError, match="could not ask GitHub"):
        sf.verify_pr_merged("u", run=boom)


# ── proposal discipline: capacity, stretch, human steps, consistent revisions ─

def _story(i, pts, human=False, stretch=None, title=None, deps=None):
    s = {"id": i, "title": title or f"title {i}", "points": pts, "acceptance": ["a"], "human_only": human, "depends_on": deps or []}
    if stretch is not None:
        s["stretch"] = stretch
    return s


def test_proposal_summary_separates_agent_work_human_steps_and_stretch_and_flags_over_capacity():
    st = [_story("S-1", 3), _story("S-2", 1, human=True), _story("S-3", 5), _story("S-4", 4, stretch=True),
          _story("S-5", 6, title="Second app — STRETCH"), _story("S-6", 1, human=True)]
    m = sf.proposal_summary(st, capacity=10)
    assert (m["agent_points"], m["human_points"], m["stretch_points"], m["committed_points"]) == (18, 2, 10, 8)
    assert m["stretch"] == ["S-4", "S-5"] and m["human"] == ["S-2", "S-6"] and m["agent"] == ["S-1", "S-3", "S-4", "S-5"]
    assert m["over_capacity"] is False and sf.proposal_summary(st, capacity=7)["over_capacity"] is True
    assert sf.proposal_summary(st, capacity=8)["over_capacity"] is False                           # exactly at capacity is fine
    assert sf.proposal_summary([_story("S-1", None)], 5)["agent_points"] == 0                       # a missing point value is zero, not a crash


def test_a_story_waits_on_the_human_steps_its_depends_on_names_until_they_are_done():
    st = [_story("S-1", 3, deps=["S-2 must be done on the mini before this is deployed", "S-9 is unrelated text for an agent story"]),
          _story("S-2", 1, human=True), _story("S-9", 3), _story("S-7", 2, deps=["S-1", "S-6"]), _story("S-6", 1, human=True)]
    assert sf.blocking_steps(st[0], st, set()) == ["S-2"]                                          # S-9 is an agent story: not a human step
    assert sf.blocking_steps(st[0], st, {"S-2"}) == []
    assert sf.blocking_steps(st[3], st, set()) == ["S-6"]                                          # S-1 is agent work, only S-6 blocks
    assert sf.blocking_steps(_story("S-2", 1, human=True, deps=["S-2"]), st, set()) == []          # never blocked by itself
    assert sf.blocking_steps(_story("S-3", 1), st, set()) == []


def test_the_product_owner_is_told_the_capacity_the_stretch_field_and_its_own_earlier_concerns_on_a_revision():
    first = sf.po_prompt("p", "1", "req", capacity=12)
    assert "roughly 12 agent points" in first and '"stretch":false' in first and "state the order in which scope is cut ONCE" in first
    assert "previous round" not in first
    rev = sf.po_prompt("p", "1", "req", previous=[{"id": "S-1"}], feedback="smaller", previous_concerns=["cut S-8 before S-11"], capacity=12)
    assert "concerns from the previous round" in rev and "cut S-8 before S-11" in rev and "keep the same scope-cut order" in rev
    assert "previous round" not in sf.po_prompt("p", "1", "req", previous=[{"id": "S-1"}], feedback="smaller")        # nothing to carry: nothing said
