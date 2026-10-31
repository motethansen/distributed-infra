"""The Scrum flow as code: Product Owner proposes stories, a human approves, the Scrum Master breaks a
story into role-tagged tasks, and the dispatcher queues them in dependency order.

Every task is queued with project / sprint / story / role, so the Scrum board can show which agent
worked which story. The pure parts (prompts, JSON parsing, plan validation, ordering) have no
network; `FleetClient` and `dispatch` talk to the orchestrator and take an injectable sleep so
they are testable against a fake queue.

Human gates: `po` and `plan` only write proposal files; `dispatch` queues nothing without `go=True`.
DeepSeek answers are text only, so a deepseek dev task is followed by an `integrator` task
(Claude Sonnet applies it, runs the tests, opens the PR). Every dev task is followed by a
`reviewer` task pinned away from the author's model family where it can be.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import httpx

TERMINAL = {"done", "failed", "needs_human"}
DEV_ROLES = {"dev_frontend", "dev_backend"}
PLANNABLE = {"analyst", "dev_frontend", "dev_backend"}        # roles the Scrum Master may assign
MAX_TASKS = 10
CARRY_CHARS = 4000                                             # of each dependency's result handed on


class PlanError(ValueError):
    """The model's output is not a plan we are willing to run."""


# ── prompts ──────────────────────────────────────────────────────────────────

def po_prompt(project: str, sprint: str, request: str, context: str = "", previous: list[dict] | None = None,
              feedback: str = "") -> str:
    ctx = ("\nContext (architecture notes and the open backlog, read-only):\n" + context.strip()) if context.strip() else ""
    rev = ""
    if previous:
        rev = ("\nYou proposed these stories earlier:\n" + json.dumps(previous, indent=1)[:14000]
               + "\n\nThe human product owner gave this feedback at sprint planning. Revise the stories to take it into account; "
               "keep story IDs that still stand, drop or merge the rest, and say in `concerns` anything in the feedback you "
               "disagree with and why:\n" + feedback.strip() + "\n")
    return f"""You are the Product Owner for the project "{project}" (sprint {sprint}).

Business request:
{request.strip()}
{ctx}{rev}
Your job is to turn the request into stories a developer can build and a reviewer can check, AND to align them with the architecture
and the existing backlog: say where a story conflicts with, duplicates or depends on something that already exists, and which
architectural rule it must respect. For each story give one user-visible outcome small enough for one sprint, points (1,2,3,5,8),
observable acceptance criteria (a response, a screen, a passing test — not "works well"), edge cases in and out of scope, dependencies,
risks, and a short BRIEF for each agent role that will work on it (dev_backend, dev_frontend, ... only the ones needed): what to build,
which files or interfaces, what not to touch. Briefs are prompts for other models: self-contained, no secrets, no credentials.
Mark stories that need a human-only step (payment dashboard, app-store console). Spend your effort on trade-offs, not on restating
the request.

Answer with ONLY a JSON object, no prose:
{{"concerns":["..."],"stories":[{{"id":"S-1","title":"...","points":3,"acceptance":["..."],"edge_cases":["..."],
"out_of_scope":["..."],"depends_on":[],"risks":["..."],"architecture_notes":"...","briefs":{{"dev_backend":"..."}},"human_only":false}}]}}"""


def groom_prompt(project: str, context: str) -> str:
    return f"""You are the Product Owner grooming the backlog of the project "{project}". Review the OPEN stories below against the
architecture notes. For each open story give a verdict: ok, needs_work (say what is missing: acceptance criteria, scope, a dependency),
duplicate (of which ID), stale (probably already done or no longer wanted — say what evidence you would check), or conflicts (with which
architectural rule). Suggest observable acceptance criteria where they are missing, and name stories that are missing altogether.
Do not invent facts about the code; where you cannot tell, say so. No secrets.

{context.strip()}

Answer with ONLY a JSON object, no prose:
{{"summary":"...","review":[{{"id":"XX-001","verdict":"ok|needs_work|duplicate|stale|conflicts","issues":["..."],
"suggested_acceptance":["..."],"brief":"..."}}],"missing":[{{"title":"...","why":"..."}}]}}"""


def load_context(viz_repo: Path, project: str, max_chars: int = 36000) -> str:
    """Architecture notes + the project's open backlog rows, capped. Architecture: the estate overview in full, any note
    named for the project, and only the TITLES of the rest (so the Product Owner can ask for them)."""
    from shared import backlog_edit
    parts: list[str] = []
    arch = viz_repo / "_architecture"
    if arch.is_dir():
        picked = [f for f in sorted(arch.glob("*.md")) if f.stem == "estate-architecture" or project.lower() in f.stem.lower()]
        for f in picked:
            parts.append(f"=== ARCHITECTURE: {f.name} ===\n" + f.read_text(encoding="utf-8", errors="replace")[:9000])
        others = [f.name for f in sorted(arch.glob("*.md")) if f not in picked]
        if others:
            parts.append("Other architecture notes (titles only): " + ", ".join(others))
    bl = viz_repo / project / "product-backlog.md"
    if bl.exists():
        from shared import scrum
        rows = [r for r in scrum.backlog_rows(bl.read_text(encoding="utf-8", errors="replace")) if r["status"] != "cancelled"]
        body = "\n".join(f"| {r['id']} | {r['title'][:160]} | {r['status']} | {r['priority']} | {r['evidence'][:240]} |" for r in rows)
        parts.append(f"=== OPEN BACKLOG: {project} ({len(rows)} open rows: id | story | status | priority | reason/evidence) ===\n" + body)
    return "\n\n".join(parts)[:max_chars]


def parse_po(text: str) -> tuple[list[dict], list[str]]:
    obj = extract_json(text)
    concerns = [str(c)[:400] for c in (obj.get("concerns") or [])] if isinstance(obj, dict) else []
    return parse_stories(text), concerns


def sm_prompt(project: str, sprint: str, story: dict, roles: dict[str, str],
              machines: dict[str, str] | None = None) -> str:
    table = "\n".join(f"- {r}: {roles[r]}" for r in sorted(roles) if r in PLANNABLE)
    mach = ("\nMachines you may prefer for a task (leave `machine` out to let the queue choose):\n"
            + "\n".join(f"- {m}: {d}" for m, d in machines.items())
            + "\nSet `machine_required: true` only when the task cannot run elsewhere (iOS build, Android build).\n") if machines else ""
    return f"""You are the Scrum Master for the project "{project}" (sprint {sprint}).
Break this story into atomic tasks for the roles below. Do not plan reviews, integration or branches: all work
for the story goes on one branch, `dis/<story id>`. After each wave an integrator automatically applies the code
that dev_backend (DeepSeek) returned as text to that branch, so later tasks can assume it is already there; never
plan an "apply the earlier results" task. File-editing tasks run one at a time. One reviewer checks the whole story
at the end.

Story:
{json.dumps(story, indent=2)}

Roles you may assign:
{table}
{mach}
Rules:
- Each task prompt must be self-contained: the agent sees only its prompt plus the results of the
  tasks it depends on. Name files, interfaces and acceptance checks. NEVER include a secret,
  token, password or connection string; refer to a credential by variable name.
- dev_backend returns code as text (it cannot edit files). Ask it for each file as a fenced block
  preceded by a line `FILE: relative/path`.
- Put the API contract first (a dev_backend task), then UI work depending on it, so they can
  proceed in parallel against the contract. Use depends_on for real ordering only.
- At most {MAX_TASKS} tasks. Choose the role by what it is best at; give dev_backend only
  self-contained logic.

Answer with ONLY a JSON object, no prose:
{{"tasks":[{{"key":"t1","title":"...","role":"dev_backend","machine":"thinkpad-x1","depends_on":[],"prompt":"..."}}]}}"""


# ── parsing ──────────────────────────────────────────────────────────────────

def extract_json(text: str) -> Any:
    """The first JSON object/array in a model's reply, tolerating fences and surrounding prose."""
    if not text or not text.strip():
        raise PlanError("empty reply")
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        return obj
    raise PlanError("no JSON found in the reply")


def parse_stories(text: str) -> list[dict]:
    obj = extract_json(text)
    stories = obj.get("stories") if isinstance(obj, dict) else obj
    if not isinstance(stories, list) or not stories:
        raise PlanError("expected a non-empty list of stories")
    out = []
    for i, s in enumerate(stories, 1):
        if not isinstance(s, dict) or not str(s.get("title") or "").strip():
            raise PlanError(f"story {i} has no title")
        acc = s.get("acceptance")
        if not isinstance(acc, list) or not [a for a in acc if str(a).strip()]:
            raise PlanError(f"story {s.get('id') or i}: no acceptance criteria")
        out.append({**s, "id": str(s.get("id") or f"S-{i}")})
    return out


def parse_plan(text: str, known_roles: set[str], known_machines: set[str] | None = None) -> list[dict]:
    """Validate the Scrum Master's task list. Unknown roles are refused, not guessed."""
    obj = extract_json(text)
    tasks = obj.get("tasks") if isinstance(obj, dict) else obj
    if not isinstance(tasks, list) or not tasks:
        raise PlanError("expected a non-empty list of tasks")
    if len(tasks) > MAX_TASKS:
        raise PlanError(f"{len(tasks)} tasks; the limit is {MAX_TASKS}")
    out, keys = [], set()
    for i, t in enumerate(tasks, 1):
        if not isinstance(t, dict):
            raise PlanError(f"task {i} is not an object")
        key = str(t.get("key") or "").strip()
        if not key or len(key) > 40 or key in keys:
            raise PlanError(f"task {i}: key {key!r} is missing, too long or repeated")
        role = str(t.get("role") or "").strip().lower()
        if role not in known_roles:
            raise PlanError(f"task {key}: unknown role {role!r}")
        if role not in PLANNABLE:
            raise PlanError(f"task {key}: role {role!r} is not assigned by the Scrum Master "
                            f"(reviews and integration are added by the dispatcher)")
        if not str(t.get("prompt") or "").strip():
            raise PlanError(f"task {key}: empty prompt")
        deps = t.get("depends_on") or []
        if not isinstance(deps, list):
            raise PlanError(f"task {key}: depends_on must be a list")
        machine = str(t.get("machine") or "").strip() or None
        if machine and known_machines is not None and machine not in known_machines:
            raise PlanError(f"task {key}: machine {machine!r} is not one of {sorted(known_machines)}")
        keys.add(key)
        out.append({"key": key, "title": str(t.get("title") or key)[:120], "role": role,
                    "depends_on": [str(d) for d in deps], "prompt": str(t["prompt"]),
                    "machine": machine, "machine_required": bool(t.get("machine_required")) and bool(machine)})
    for t in out:
        for d in t["depends_on"]:
            if d not in keys:
                raise PlanError(f"task {t['key']}: depends on unknown task {d!r}")
            if d == t["key"]:
                raise PlanError(f"task {t['key']}: depends on itself")
    waves(out)                                                   # raises on a cycle
    return out


def waves(tasks: list[dict]) -> list[list[dict]]:
    """Dependency layers: every task in layer n depends only on layers before n."""
    remaining, done, layers = list(tasks), set(), []
    while remaining:
        layer = [t for t in remaining if all(d in done for d in t["depends_on"])]
        if not layer:
            raise PlanError("dependency cycle among: " + ", ".join(t["key"] for t in remaining))
        layers.append(layer)
        done |= {t["key"] for t in layer}
        remaining = [t for t in remaining if t["key"] not in done]
    return layers


# ── orchestrator client ──────────────────────────────────────────────────────

class FleetClient:
    """Minimal sync client; tries each orchestrator in turn on connection errors and 5xx."""

    def __init__(self, urls: list[str], key: str, transport: httpx.BaseTransport | None = None,
                 client: str = "scrum-cli"):
        self.urls, self.key, self.transport, self.client = [u.rstrip("/") for u in urls], key, transport, client

    def _call(self, method: str, path: str, **kw) -> Any:
        errs = []
        for base in self.urls:
            try:
                with httpx.Client(timeout=20, transport=self.transport) as c:
                    r = c.request(method, base + path, headers={"x-secret-key": self.key, "x-client": self.client}, **kw)
            except httpx.HTTPError as e:
                errs.append(f"{base}: {type(e).__name__}")
                continue
            if r.status_code >= 500:
                errs.append(f"{base}: HTTP {r.status_code}")
                continue
            if r.status_code >= 400:
                raise PlanError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
            return r.json()
        raise PlanError("no orchestrator reachable — " + "; ".join(errs))

    def queue(self, payload: dict, notes: str) -> dict:
        return self._call("POST", "/tasks", json={"type": "agent_run", "payload": payload, "notes": notes[:80]})

    def get(self, task_id: str) -> dict:
        return self._call("GET", f"/tasks/{task_id}")

    def usage(self, group_by: str) -> dict:
        return self._call("GET", "/scrum/usage", params={"group_by": group_by})

    def wait(self, task_ids: list[str], timeout: float = 1800, poll: float = 3,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, dict]:
        end, left, out = time.monotonic() + timeout, set(task_ids), {}
        while left:
            for tid in sorted(left):
                t = self.get(tid)
                if t.get("status") in TERMINAL:
                    out[tid] = t
                    left.discard(tid)
            if left:
                if time.monotonic() > end:
                    raise PlanError("timed out waiting for: " + ", ".join(sorted(left)))
                sleep(poll)
        return out


def answer(task: dict) -> str:
    r = task.get("result") or {}
    return str(r.get("response") or r.get("output") or "")


def tags(project: str, sprint: str, story: str | None, role: str, approved: bool, as_agent: str | None,
         cwd: str | None, machine: str | None = None, machine_required: bool = False, **extra) -> dict:
    p = {"project": project, "sprint": sprint, "role": role}
    if story:
        p["story"] = story
    if approved:
        p["approved"] = True
    if as_agent:
        p["agent"] = as_agent
    if cwd:
        p["cwd"] = cwd
    if machine:
        p["_target_machine" if machine_required else "_preferred_machine"] = machine
    p.update(extra)
    return p


# ── the three steps ──────────────────────────────────────────────────────────

def propose_stories(c: FleetClient, project: str, sprint: str, request: str, context: str = "",
                    approved: bool = False, as_agent: str | None = None, sleep=time.sleep,
                    previous: list[dict] | None = None, feedback: str = "") -> tuple[list[dict], dict]:
    """Returns (stories, task). `task["concerns"]` carries the Product Owner's concerns about the request."""
    t = c.queue(tags(project, sprint, None, "product_owner", approved, as_agent, None,
                     prompt=po_prompt(project, sprint, request, context, previous, feedback), timeout=900), f"PO {project} {sprint}")
    done = c.wait([t["id"]], sleep=sleep)[t["id"]]
    if done["status"] != "done":
        raise PlanError(f"product owner task {done['status']}: {done.get('notes') or ''}")
    stories, concerns = parse_po(answer(done))
    done["concerns"] = concerns
    return stories, done


def groom_backlog(c: FleetClient, project: str, context: str, approved: bool = False, as_agent: str | None = None,
                  sleep=time.sleep) -> tuple[dict, dict]:
    t = c.queue(tags(project, "grooming", None, "product_owner", approved, as_agent, None,
                     prompt=groom_prompt(project, context), timeout=900), f"groom {project}")
    done = c.wait([t["id"]], sleep=sleep)[t["id"]]
    if done["status"] != "done":
        raise PlanError(f"product owner task {done['status']}: {done.get('notes') or ''}")
    obj = extract_json(answer(done))
    if not isinstance(obj, dict) or not isinstance(obj.get("review"), list):
        raise PlanError("the grooming reply has no review list")
    return obj, done


def plan_story(c: FleetClient, project: str, sprint: str, story: dict, roles: dict[str, str],
               approved: bool = False, as_agent: str | None = None, sleep=time.sleep,
               machines: dict[str, str] | None = None) -> tuple[list[dict], dict]:
    t = c.queue(tags(project, sprint, story["id"], "scrum_master", approved, as_agent, None,
                     prompt=sm_prompt(project, sprint, story, roles, machines), timeout=900), f"SM {project} {story['id']}")
    done = c.wait([t["id"]], sleep=sleep)[t["id"]]
    if done["status"] != "done":
        raise PlanError(f"scrum master task {done['status']}: {done.get('notes') or ''}")
    return parse_plan(answer(done), set(roles), set(machines) if machines else None), done


def _carry(results: dict[str, str], deps: list[str]) -> str:
    if not deps:
        return ""
    parts = [f"--- result of task {d} ---\n{results.get(d, '')[:CARRY_CHARS]}" for d in deps]
    return "\n\nResults of the tasks this one depends on:\n" + "\n\n".join(parts)


def integrate_prompt(story: dict, code: list[tuple[dict, str]]) -> str:
    parts = "\n\n".join(f"--- task {t['key']}: {t['title']} ---\n{text[:12000]}" for t, text in code)
    return (f"Story {story['id']}: {story['title']}\n\nAnother agent (DeepSeek) returned the code below as text, one block per "
            "task, in order. Apply ALL of it to the story branch (create the files at the FILE: paths, adapting imports to the "
            "repo's conventions), run the tests, fix only what is needed to make the story's acceptance criteria hold, "
            "commit, push and open the PR if none exists. Do not merge or deploy. Report the PR URL, the test output, and anything you "
            f"changed from the returned code.\n\nAcceptance criteria:\n{json.dumps(story.get('acceptance', []))}\n\n"
            f"Returned code:\n{parts}")


def validate_prompt(story: dict) -> str:
    crit = "\n".join(f"{i}. {c}" for i, c in enumerate(story.get("acceptance", []), 1))
    return (f"You are the independent tester for story {story['id']}: {story['title']}. You did not write the code and must not "
            "change it. Work on the story branch `dis/" + story["id"] + "`.\n\n"
            "1. Write tests from the ACCEPTANCE CRITERIA below and the public interfaces only: decide what to assert from the "
            "criteria, not by reading how the implementation works. Follow the repository's test conventions; put them in new files "
            "named for the story. Commit and push them to the story branch.\n"
            "2. Run the repository's FULL test suite on that branch. If anything fails, do not fix the implementation; report it.\n"
            "3. For each criterion decide pass, fail or unverified (unverified = it needs a live system, a human, or a secret you do not "
            "have). Give one line of evidence each: the test name and its result, or why it could not be checked.\n\n"
            f"Acceptance criteria:\n{crit}\n\n"
            "End your reply with ONE JSON object and nothing after it:\n"
            '{"validation":{"suite":"<N passed, M failed>","criteria":[{"n":1,"status":"pass|fail|unverified","evidence":"..."}]}}')


_VALID_STATUS = {"pass", "fail", "unverified"}


def parse_validation(text: str, n_criteria: int | None = None) -> dict:
    """The tester's verdict. `passed` is computed here, never taken from the model: every criterion
    must be `pass` (and, when n_criteria is known, every criterion must be reported)."""
    dec, found = json.JSONDecoder(), None
    for m in re.finditer(r"\{", text or ""):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("validation"), dict):
            found = obj["validation"]
    if not found or not isinstance(found.get("criteria"), list) or not found["criteria"]:
        return {"passed": None, "error": "the tester did not return a validation block", "counts": {}, "criteria": []}
    crit = []
    for c in found["criteria"]:
        if isinstance(c, dict):
            st = str(c.get("status") or "").strip().lower()
            crit.append({"n": c.get("n"), "status": st if st in _VALID_STATUS else "unverified",
                         "evidence": str(c.get("evidence") or "")[:300]})
    counts = {k: sum(1 for c in crit if c["status"] == k) for k in ("pass", "fail", "unverified")}
    complete = n_criteria is None or len({c["n"] for c in crit}) >= n_criteria
    return {"passed": bool(crit) and counts["pass"] == len(crit) and complete, "complete": complete, "counts": counts,
            "suite": str(found.get("suite") or "")[:120], "criteria": crit}


def validation_summary(v: dict | None) -> str:
    if not v:
        return "no validation ran"
    if v.get("passed") is None:
        return v.get("error") or "validation unavailable"
    c = v["counts"]
    return f"{c['pass']} pass, {c['fail']} fail, {c['unverified']} unverified" + (f" ({v['suite']})" if v.get("suite") else "") \
        + ("" if v.get("complete", True) else "; some criteria were not reported")


def review_prompt(story: dict, author: str, material: str, validation: dict | None = None) -> str:
    return (f"Independent review of work on story {story['id']}: {story['title']}. The author was {author}; you did not "
            "write it and must not edit anything. Check it against the acceptance criteria, run the tests if there is a "
            "branch or PR, and report findings ranked by consequence (wrong behaviour, missing test, security, style). "
            f"Say what you could not verify.\n\nAcceptance criteria:\n{json.dumps(story.get('acceptance', []))}\n\n"
            f"Independent tester's verdict: {validation_summary(validation)}\n\n"
            f"Material to review (PR URL / returned code):\n{material[:12000]}")


def repo_preamble(project: str, cfg: dict | None, story_id: str) -> str:
    """Tell the agent where the code is: workers keep clones in different places, so find before cloning."""
    if not cfg:
        return ""
    note = f" Note: {cfg['note']}." if cfg.get("note") else ""
    return (f"Repository for project {project}: {cfg['repo']} (folder name `{cfg['dir']}`). First look for an existing clone "
            f"on this machine (`find ~/Projects -maxdepth 4 -type d -name {cfg['dir']} -exec test -d {{}}/.git \\; -print`); "
            f"if there is none, clone it into ~/Projects/{cfg['dir']}. All work for this story goes on ONE branch, `dis/{story_id}`: "
            f"fetch, check it out if it exists (else create it from the default branch), `git pull --ff-only`, commit and push to it. "
            f"If no pull request exists for that branch open one with `gh pr create`; otherwise just push. Never merge, never deploy, "
            f"never touch a live database."
            f"{note}\n\n")


def _agent_of(task: dict) -> str | None:
    return task.get("agent") or (task.get("result") or {}).get("agent")


def _text_only(role: str, project: str, prompt: str) -> bool:
    """Roles that never touch the repository: DeepSeek developers (text back) and analysts."""
    if role == "analyst":
        return True
    if role != "dev_backend":
        return False
    from agents import roles as scrum_roles
    try:
        return scrum_roles.resolve(role, project=project, prompt=prompt)["agent"] == "deepseek"
    except scrum_roles.RoleError:
        return False


def dispatch(c: FleetClient, project: str, sprint: str, story: dict, tasks: list[dict], *, go: bool,
             approved: bool = False, cwd: str | None = None, sleep=time.sleep, say=print,
             project_cfg: dict | None = None) -> list[dict]:
    """Run the plan wave by wave; stop at the first failed task. Returns a log of
    {key, role, task_id, status, agent}. With go=False nothing is queued.

    One branch per story (dis/<story>). Tasks that only return text (DeepSeek, analyst) run in parallel;
    after each wave ONE integrator applies all the DeepSeek code of that wave to the branch; tasks that
    edit files run one at a time so they never race on the branch. After the last wave ONE reviewer
    checks the whole story, pinned off Codex's family when Codex wrote any of it."""
    layers = waves(tasks)
    if not go:
        for i, layer in enumerate(layers, 1):
            say(f"wave {i}: " + ", ".join(f"{t['key']}[{t['role']}]" for t in layer))
        say("dry run: nothing queued. Re-run with --go to queue these (integration and the story review are added automatically).")
        return []
    log: list[dict] = []
    results: dict[str, str] = {}
    authors: set[str] = set()
    pr_material: list[str] = []

    def run(role: str, prompt: str, key: str, label: str, machine=None, required=False, as_agent=None) -> dict:
        r = c.queue(tags(project, sprint, story["id"], role, approved, as_agent, cwd, machine, required,
                         prompt=prompt, timeout=1800, flow_key=key), label)
        say(f"queued {key} [{role}] -> {r['id'][:8]}")
        done = c.wait([r["id"]], sleep=sleep)[r["id"]]
        ran = _agent_of(done)
        log.append({"key": key, "role": role, "task_id": r["id"], "status": done["status"], "agent": ran})
        say(f"{key} [{role}] {done['status']} on {ran or '?'}")
        if done["status"] != "done":
            raise PlanError(f"task {key} ended {done['status']}: {done.get('notes') or ''} — stopped; fix and re-run")
        return done

    def batch(group: list[dict]) -> dict[str, dict]:
        queued = {}
        for t in group:
            prompt = t["prompt"] + _carry(results, t["depends_on"])
            r = c.queue(tags(project, sprint, story["id"], t["role"], approved, None, cwd, t.get("machine"),
                             t.get("machine_required", False), prompt=prompt, timeout=1800, flow_key=t["key"]),
                        f"{t['role']} {story['id']} {t['key']}")
            queued[r["id"]] = t
            say(f"queued {t['key']} [{t['role']}] -> {r['id'][:8]}")
        out = {}
        for tid, done in c.wait(list(queued), sleep=sleep).items():
            t, ran = queued[tid], _agent_of(done)
            log.append({"key": t["key"], "role": t["role"], "task_id": tid, "status": done["status"], "agent": ran})
            say(f"{t['key']} [{t['role']}] {done['status']} on {ran or '?'}")
            if done["status"] != "done":
                raise PlanError(f"task {t['key']} ended {done['status']}: {done.get('notes') or ''} — stopped; fix and re-run")
            results[t["key"]] = answer(done)
            if ran and t["role"] in DEV_ROLES:
                authors.add(ran)
            out[t["key"]] = done
        return out

    for layer in layers:
        parallel = [t for t in layer if _text_only(t["role"], project, t["prompt"])]
        serial = [t for t in layer if t not in parallel]
        done = batch(parallel) if parallel else {}
        code = [(t, answer(done[t["key"]])) for t in parallel if t["role"] == "dev_backend"
                and _agent_of(done[t["key"]]) == "deepseek"]
        if code:
            pre = repo_preamble(project, project_cfg, story["id"])
            d = run("integrator", pre + integrate_prompt(story, code), "integrate:" + "+".join(t["key"] for t, _ in code),
                    f"integrate {story['id']}")
            pr_material.append(answer(d))
            if _agent_of(d):
                authors.add(_agent_of(d))
        for t in serial:       # file editors one at a time: same branch, no races
            pre = repo_preamble(project, project_cfg, story["id"]) if t["role"] in DEV_ROLES else ""
            d = batch([{**t, "prompt": pre + t["prompt"]}])[t["key"]]
            pr_material.append(answer(d))
    validation = None
    if any(t["role"] in DEV_ROLES for t in tasks):
        pre = repo_preamble(project, project_cfg, story["id"])
        try:
            d = run("tester", pre + validate_prompt(story), "validate", f"validate {story['id']}")
            validation = parse_validation(answer(d), len(story.get("acceptance", [])) or None)
        except PlanError as e:
            validation = {"passed": None, "error": f"the tester task failed: {e}", "counts": {}, "criteria": []}
        log[-1]["validation"] = validation
        say("validation: " + validation_summary(validation))
        pin = "claude" if "codex" in authors else None
        material = "\n\n".join(pr_material)
        d = run("reviewer", review_prompt(story, ", ".join(sorted(authors)), material, validation), "review", f"review {story['id']}", as_agent=pin)
        rran = _agent_of(d)
        log[-1].update({"authors": sorted(authors), "same_family": bool(rran and rran in authors)})
        if rran and rran in authors:
            say(f"review ran on {rran}, the SAME model family as an author")
    return log


def save(base: Path, project: str, sprint: str, name: str, data: Any) -> Path:
    d = base / "data" / "scrum" / project / sprint
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return p


# ── human approval of a plan ─────────────────────────────────────────────────

def approve_plan(path: Path, who: str, now: str) -> dict:
    """Record that a human approved this exact plan. Editing the tasks afterwards voids it."""
    plan = json.loads(path.read_text())
    plan["approval"] = {"by": who, "at": now, "digest": plan_digest(plan)}
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
    return plan


def plan_digest(plan: dict) -> str:
    import hashlib
    return hashlib.sha256(json.dumps([plan.get("story"), plan.get("tasks")], sort_keys=True).encode()).hexdigest()[:16]


def is_approved(plan: dict) -> bool:
    a = plan.get("approval")
    return bool(a and a.get("by") and a.get("digest") == plan_digest(plan))


# ── sprint review ────────────────────────────────────────────────────────────

_PR = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def sprint_review(tasks: list[dict], project: str, sprint: str, board_sprint: dict | None = None) -> tuple[dict, str]:
    """What the fleet did for one sprint, from the queue rows tagged with it. Returns (data, markdown)."""
    mine = [t for t in tasks if (t.get("payload") or {}).get("project") == project and str((t.get("payload") or {}).get("sprint")) == sprint]
    by = lambda f: {k: sum(1 for t in mine if f(t) == k) for k in sorted({f(t) for t in mine}, key=str)}   # noqa: E731
    prs = sorted({m for t in mine for m in _PR.findall(json.dumps(t.get("result") or {}))})
    tok_out = sum(t.get("tokens_out") or 0 for t in mine)
    tok_in = sum(t.get("tokens_in") or 0 for t in mine)
    cost = round(sum(t.get("cost_usd") or 0 for t in mine), 4)
    stuck = [t for t in mine if t.get("status") in ("failed", "needs_human")]
    data = {"project": project, "sprint": sprint, "tasks": len(mine), "by_role": by(lambda t: t.get("role") or "(none)"),
            "by_agent": by(lambda t: t.get("agent") or "(none)"), "by_machine": by(lambda t: t.get("assigned_to") or "(none)"),
            "by_status": by(lambda t: t.get("status")), "tokens_in": tok_in, "tokens_out": tok_out, "cost_usd": cost,
            "pull_requests": prs, "needs_attention": [{"id": t["id"][:8], "role": t.get("role"), "notes": (t.get("notes") or "")[:200]} for t in stuck]}
    L = [f"# Sprint review — {project} {sprint}", "",
         f"{len(mine)} fleet tasks · {data['by_status']} · tokens in {tok_in:,} (incl. cached) / out {tok_out:,} · est. cost ${cost:.2f}", ""]
    if board_sprint:
        L += [f"Board: {board_sprint.get('points_done')} of {board_sprint.get('points_total')} points done, "
              f"{board_sprint.get('points_remaining')} remaining.", ""]
    for title, key in (("Roles", "by_role"), ("Agents", "by_agent"), ("Machines", "by_machine")):
        L += [f"**{title}:** " + ", ".join(f"{k} {v}" for k, v in data[key].items()), ""]
    L += ["## Pull requests to review", ""] + ([f"- {u}" for u in prs] or ["- none opened by the fleet this sprint"]) + [""]
    L += ["## Needs your attention", ""] + ([f"- {x['id']} ({x['role']}): {x['notes']}" for x in data["needs_attention"]] or ["- nothing stuck"]) + [""]
    return data, "\n".join(L)


# ── backlog sync: open / in progress / in review automatically, closed only on confirmation ─────────

def _git(repo: Path, *args: str, run=subprocess.run) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)


def edit_file(repo: Path, rel: str, fn, message: str, run=subprocess.run, create: bool = False) -> dict:
    """Apply fn(text) -> (new_text, result) to <repo>/<rel>, commit that one file and try to push. A failed push is
    reported, not fatal: the commit is kept for the next push. Refuses to run over uncommitted changes (a human may be editing)."""
    from shared import backlog_edit, sprint_edit
    path = repo / rel
    if path.exists():
        if _git(repo, "status", "--porcelain", "--", rel, run=run).stdout.strip():
            raise PlanError(f"{path.name} has uncommitted changes (you may be editing it); commit or stash them first")
        text = path.read_text(encoding="utf-8")
    elif create:
        text = ""
    else:
        raise PlanError(f"{path} not found; set VIZNEO_REPO to the vizneoprojects checkout")
    try:
        new, result = fn(text)
    except (backlog_edit.BacklogError, sprint_edit.SprintError) as e:
        raise PlanError(str(e)) from e
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    _git(repo, "add", rel, run=run)
    c = _git(repo, "commit", "-q", "-m", message, "--", rel, run=run)
    if c.returncode != 0:
        raise PlanError("git commit failed: " + (c.stderr or c.stdout).strip()[:200])
    pushed = _git(repo, "push", "-q", "origin", "HEAD", run=run)
    return {"result": result, "committed": True, "pushed": pushed.returncode == 0,
            "push_error": None if pushed.returncode == 0 else (pushed.stderr or "").strip().splitlines()[-1:] or ["push failed"]}


def edit_backlog(repo: Path, project: str, fn, message: str, run=subprocess.run) -> dict:
    return edit_file(repo, f"{project}/product-backlog.md", fn, message, run=run)


def register_story(repo: Path, project: str, story: dict, who: str, day: str, plan_name: str, run=subprocess.run) -> dict:
    from shared import backlog_edit
    ev = f"proposed by the Product Owner, approved by {who} {day}; plan {plan_name}"
    r = edit_backlog(repo, project, lambda t: backlog_edit.add_story(t, story["title"], evidence=ev),
                     f"{project}: story registered as planned (dis-agent, approved by {who})", run=run)
    r["id"] = r["result"]
    return r


def mark_story(repo: Path, project: str, bid: str, status: str, evidence: str | None, run=subprocess.run) -> dict:
    from shared import backlog_edit
    return edit_backlog(repo, project, lambda t: (backlog_edit.set_status(t, bid, status, evidence), None),
                        f"{project}: {bid} {status} (dis-agent)", run=run)


def close_story(repo: Path, project: str, bid: str, who: str, day: str, evidence: str, run=subprocess.run) -> dict:
    """Close a story. Only call this after the human confirmed at a sprint review."""
    from shared import backlog_edit
    if not backlog_edit.status_of((repo / project / "product-backlog.md").read_text(encoding="utf-8"), bid).startswith("🔍"):
        raise PlanError(f"{bid} is not in review; only a story whose work is finished and under review can be closed")
    return edit_backlog(repo, project, lambda t: (backlog_edit.set_status(t, bid, f"✅ done {day}", f"{evidence}; closed after sprint review, confirmed by {who}", closing=True), None),
                        f"{project}: {bid} closed after sprint review, confirmed by {who}", run=run)


def story_evidence(tasks: list[dict], project: str, sprint: str, story_id: str) -> dict:
    mine = [t for t in tasks if (t.get("payload") or {}).get("project") == project
            and str((t.get("payload") or {}).get("sprint")) == sprint and (t.get("payload") or {}).get("story") == story_id
            and (t.get("payload") or {}).get("role") not in ("product_owner", "scrum_master")]
    prs = sorted({m for t in mine for m in _PR.findall(json.dumps(t.get("result") or {}))})
    review = next((t for t in sorted(mine, key=lambda t: t.get("created_at") or "", reverse=True)
                   if (t.get("payload") or {}).get("role") == "reviewer" and t.get("status") == "done"), None)
    agents = sorted({t.get("agent") for t in mine if t.get("agent")})
    tester = next((t for t in sorted(mine, key=lambda t: t.get("created_at") or "", reverse=True)
                   if (t.get("payload") or {}).get("role") == "tester"), None)
    validation = (parse_validation(answer(tester)) if tester and tester.get("status") == "done"
                  else ({"passed": None, "error": "the tester task did not finish", "counts": {}, "criteria": []} if tester else None))
    return {"validation": validation, "tasks": len(mine), "done": sum(1 for t in mine if t.get("status") == "done"),
            "stuck": [t["id"][:8] for t in mine if t.get("status") in ("failed", "needs_human")],
            "agents": agents, "pull_requests": prs, "reviewed": review is not None,
            "review_excerpt": answer(review)[:700] if review else ""}


def cancel_story(repo: Path, project: str, bid: str, who: str, day: str, reason: str, run=subprocess.run) -> dict:
    """Mark a story cancelled (a human decision, with a reason). It stays visible for 60 days, then drops off the board."""
    from shared import backlog_edit
    if not reason.strip():
        raise PlanError("a cancellation needs a reason")
    return edit_backlog(repo, project, lambda t: (backlog_edit.set_status(t, bid, f"❌ cancelled {day}", f"cancelled by {who}: {reason.strip()}", closing=True), None),
                        f"{project}: {bid} cancelled by {who}", run=run)


# ── the sprint file the fleet maintains ──────────────────────────────────────

def sprint_open_story(repo: Path, project: str, number: int, story: dict, bid: str, request: str, day: str, run=subprocess.run) -> dict:
    """Add the approved story to sprint-NN.md (creating the file if this is the sprint's first story)."""
    from shared import sprint_edit as se

    def fn(text: str):
        text = text or se.new_sprint(project, number, request, day)
        text = se.add_story(text, bid, story["title"], float(story.get("points") or 0))
        return se.record_burndown(text, day), None
    return edit_file(repo, se.sprint_file(project, number), fn, f"{project}: sprint {number:02d} — {bid} planned (dis-agent)", run=run, create=True)


def sprint_mark(repo: Path, project: str, number: int, bid: str, status: str, day: str, closing: bool = False, run=subprocess.run) -> dict:
    from shared import sprint_edit as se

    def fn(text: str):
        return se.record_burndown(se.set_story_status(text, bid, status, day, closing=closing), day), None
    return edit_file(repo, se.sprint_file(project, number), fn, f"{project}: sprint {number:02d} — {bid} {status} (dis-agent)", run=run)


def find_sprint_of(repo: Path, project: str, bid: str) -> int | None:
    """The number of the dis-agent sprint file holding this story, if any."""
    from shared import sprint_edit as se
    for f in sorted((repo / project).glob("sprint-*.md")):
        t = f.read_text(encoding="utf-8")
        if se.owner_of(t) == se.OWNER and re.search(rf"\b{re.escape(bid)}\b", t):
            m = re.match(r"sprint-0*(\d+)\.md$", f.name)
            return int(m.group(1)) if m else None
    return None
