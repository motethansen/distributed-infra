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
                               "result": {"ok": not bad, "agent": agent, "response": f"output of {p.get('flow_key') or p['role']}"}}
            self.order.append(tid)
            self.events.append(("POST", p.get("flow_key") or p["role"]))
            return httpx.Response(201, json={"id": tid})
        if req.method == "GET" and req.url.path.startswith("/tasks/"):
            t = self.tasks[req.url.path.rsplit("/", 1)[1]]
            self.events.append(("GET", t["payload"].get("flow_key") or t["payload"]["role"]))
            return httpx.Response(200, json=t)
        return httpx.Response(404)

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
    assert q.payloads()[1].get("agent") == "claude"
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
    assert [p["role"] for p in q.payloads()] == ["dev_backend", "reviewer"]


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
    assert [p["role"] for p in q.payloads()] == ["dev_frontend", "reviewer"]
    q2 = Queue(agent_for=lambda p: "agy")
    _run(q2, [_t("doc", "analyst")])
    assert [p["role"] for p in q2.payloads()] == ["analyst"]


def test_an_analyst_is_not_an_author_of_the_code_under_review():
    q = Queue(agent_for=lambda p: {"dev_frontend": "claude"}.get(p["role"], "codex"))
    out, _ = _run(q, [_t("ui", "dev_frontend"), _t("doc", "analyst")])
    assert out[-1]["authors"] == ["claude"] and out[-1]["same_family"] is False
    assert q.payloads()[-1].get("agent") is None
