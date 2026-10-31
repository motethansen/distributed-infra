#!/usr/bin/env python3
"""dis-agent — one command for the distributed agents.

  dis-agent                       chat (switch LLM with /model, /role, /machine; switching keeps the conversation)
  dis-agent ask "question"        one question, one answer
  dis-agent roles                 who holds which role + live usage
  dis-agent status                open sprints, remaining points, what the fleet is doing
  dis-agent po   -p PROJ -s SPR "request"         Product Owner (Opus) proposes stories, aligned with the architecture and backlog
  dis-agent po   -p PROJ -s SPR --revise "..."    ...revises them with your sprint-planning feedback
  dis-agent planning -p PROJ -s SPR               interactive sprint planning: stories, your feedback, revise, until you accept
  dis-agent groom -p PROJ                         Product Owner reviews the open backlog against the architecture (writes nothing)
  dis-agent plan -p PROJ -s SPR --story ID        Scrum Master plans roles, machines, order (nothing queued)
  dis-agent approve PLAN                          you approve exactly that plan
  dis-agent go PLAN                               run an approved plan (dry run unless approved)
  dis-agent next [-p PROJ]                        what to do next: ranked suggestions, each with the exact command, effect and cost
  dis-agent dismiss -p PROJ -s SPR                stop `next` offering a sprint proposal you will not pursue (files are kept)
  dis-agent steps -p PROJ -s SPR                  YOUR steps in a sprint proposal (human-only stories) with what each blocks
  dis-agent done -p PROJ -s SPR --story S-2 --evidence "..."   mark one of your steps done
  dis-agent backlog -p PROJ                       the product backlog arranged per sprint, with why each blocked story is blocked
  dis-agent cancel -p PROJ --story ID --reason ..  cancel a story (you decide; shown for 60 days, then hidden)
  dis-agent scrum [--narrate]                     scrum of scrums across every project: review queue, decisions, stalls, adjustments
  dis-agent review                                the review queue: each waiting story with its sprint, PRs and the exact command to confirm it
  dis-agent accept -p PROJ --story ID --pr URL    confirm a story the fleet did not build (GitHub must show the PR merged), or --evidence "..."
  dis-agent review -p PROJ -s SPR                 sprint review: what ran, validation, PRs to look at, what is stuck
  dis-agent validate -p PROJ -s SPR --story ID    run the independent tester (agy) on a story; --branch main when it is already merged
  dis-agent close  -p PROJ -s SPR [--story ID]    close finished stories in the backlog — only after you confirm

A story is open (planned) once you approve its plan, in progress while `go` runs, in review when the work
is finished, and closed only by `close` after you have read the review. VIZNEO_REPO points at the
vizneoprojects checkout whose backlog is edited (default ~/Projects/github/techstartups/vizneoprojects).

Everything it queues carries project/sprint/story/role, so Vantage's Scrum page shows who did what.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "clients" / "fleet-mcp"))

import yaml  # noqa: E402

import scrum_cli as sc  # noqa: E402
from shared import scrum_flow as sf  # noqa: E402

# What you type after /model -> (agent, role). Opus is only reachable through the capped product_owner role.
MODELS = {
    "auto": (None, None), "claude": ("claude", None), "sonnet": ("claude", None), "codex": ("codex", None),
    "agy": ("agy", None), "gemini": ("agy", None), "deepseek": ("deepseek", None), "opus": (None, "product_owner"),
}
HELP = """/model auto|claude|codex|agy|deepseek|opus   pick who answers (opus = the capped Product Owner role)
/role <name>        answer as a Scrum role (scrum_master, dev_backend, reviewer, analyst ...); /role off
/machine <name>     prefer a machine (mac-mini, thinkpad-x1); /machine auto
/project <name>     tag what you ask with a project (shows on the Scrum board); /project off
/new                forget this conversation          /who   show current settings
/help               this text                          /quit  leave"""


def viz_repo() -> Path:
    return Path(os.environ.get("VIZNEO_REPO", "~/Projects/github/techstartups/vizneoprojects")).expanduser()


def _refresh_board() -> None:
    """After a pushed change, make the board re-read the notes repo now and say whether it has our commit. Never fatal."""
    try:
        head = subprocess.run(["git", "-C", str(viz_repo()), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        got = (sc.client()._call("POST", "/scrum/refresh") or {}).get("head") or ""
        if head and got and (got.startswith(head) or head.startswith(got)):
            print(f"  board: refreshed, it is now reading {got}: your change is visible")
        else:
            print(f"  board: asked to refresh but it reads {got or 'an older commit'}, not {head}; it catches up within ~3 minutes")
    except Exception as e:                                   # noqa: BLE001 — a refresh problem must not look like a failed change
        print(f"  board: could not be refreshed now ({type(e).__name__}); it catches up within ~3 minutes")


def _report(what: str, r: dict) -> None:
    print(f"  backlog: {what} (committed{', pushed' if r['pushed'] else '; NOT pushed — ' + ' '.join(r['push_error'] or []) + ' — push when online'})")
    if r["pushed"]:
        _refresh_board()


def _sprint_number(project: str, label: str) -> int:
    """Sprint label -> sprint file number, remembered in data/scrum/<project>/sprints.json."""
    from shared import sprint_edit
    f = ROOT / "data" / "scrum" / project / "sprints.json"
    mapping = json.loads(f.read_text()) if f.exists() else {}
    n = sprint_edit.number_for(viz_repo(), project, label, mapping)
    if not label.strip().isdigit() and label not in mapping:
        mapping[label] = n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(mapping, indent=2))
    return n


def _sprint_maps(project: str) -> dict:
    f = ROOT / "data" / "scrum" / project / "sprints.json"
    return json.loads(f.read_text()) if f.exists() else {}


def next_sprint(project: str) -> str:
    """The next free sprint number as the board shows it (the number a new sprint file will get)."""
    from shared import sprint_edit
    return str(sprint_edit.number_for(viz_repo(), project, "__new__", _sprint_maps(project)))


def resolve_sprint(project: str, s: str) -> str:
    """The folder name holding the plans: accept the sprint's label (S-roles) or the number the board shows (1)."""
    base = ROOT / "data" / "scrum" / project
    if (base / s).is_dir():
        return s
    for label, n in _sprint_maps(project).items():
        if str(n) == s.lstrip("0") and (base / label).is_dir():
            return label
    have = sorted(d.name for d in base.iterdir() if d.is_dir() and any(d.glob("*.plan.json"))) if base.is_dir() else []
    sys.exit(f"no plans for sprint {s!r} in {project}. Sprints with plans: {', '.join(have) or 'none'}. "
             f"Use the label or the number the board shows (a label's number: {json.dumps(_sprint_maps(project)) if _sprint_maps(project) else 'none yet'}).")


def _sprint_step(what: str, fn) -> bool:
    """A sprint-file update must never break the flow it rides on: say what happened and carry on. True if it worked."""
    try:
        _report(what, fn())
        return True
    except sf.PlanError as e:
        print(f"  sprint file NOT updated: {e}")
        return False


def projects_cfg() -> dict:
    return yaml.safe_load((ROOT / "config" / "scrum_projects.yaml").read_text())


def _tools():
    import server
    cfg = server.load_config()
    cfg["client"] = "dis-agent"
    return server.Tools(server.Fleet(cfg), server.State(cfg["state_file"]))


def _show(r: dict, tools_name: str = "") -> str:
    if r.get("error"):
        return f"! {r['error']}"
    out = str(r.get("response") or r.get("note") or r)
    tag = ""
    for f in r.get("failover") or []:
        tag += f"\n  ↪ {f.get('agent')} skipped ({f.get('reason')}{', back ' + f['until'] if f.get('until') else ''})"
    return f"{out}\n  — {r.get('answered_by') or '?'}{tag}"


async def _ask(tools, prompt, st) -> dict:
    agent, role = MODELS[st["model"]]
    role = st["role"] or role
    kw = dict(agent=None if role else agent, role=role, machine=st["machine"], project=st["project"], conversation=st["conv"])
    r = await tools.ask(prompt, wait_seconds=100, **kw)
    while r.get("task_id") and r.get("status") not in ("done", "failed", "needs_human") and "error" not in r:
        print("  … still working", flush=True)
        r = await tools.result(r["task_id"], 100)
    return r


def cmd_chat(a) -> None:
    tools = _tools()
    st = {"model": a.model, "role": None, "machine": None, "project": a.project, "conv": f"dis-agent-{os.getpid()}"}
    print("dis-agent chat — /help for commands, /quit to leave. Answers show who produced them.")
    while True:
        try:
            line = input(f"[{st['role'] or st['model']}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            arg = arg.strip()
            if cmd in ("quit", "exit", "q"):
                return
            elif cmd == "help":
                print(HELP)
            elif cmd == "model" and arg in MODELS:
                st["model"], st["role"] = arg, None
                print("  → " + ("the capped Product Owner role (Opus)" if arg == "opus" else f"{arg}") + " (the conversation carries over)")
            elif cmd == "role":
                if arg in ("", "off"):
                    st["role"] = None
                elif arg in sc.role_hints():
                    st["role"] = arg
                else:
                    print(f"  roles: {', '.join(sc.role_hints())}")
            elif cmd == "machine":
                st["machine"] = None if arg in ("", "auto") else arg
            elif cmd == "project":
                st["project"] = None if arg in ("", "off") else arg
            elif cmd == "new":
                st["conv"] = f"dis-agent-{os.getpid()}-{int(datetime.now().timestamp())}"
                print("  new conversation")
            elif cmd == "who":
                print("  " + ", ".join(f"{k}={v}" for k, v in st.items() if k != "conv"))
            else:
                print("  unknown; /help")
            continue
        print(_show(asyncio.run(_ask(tools, line, st))))


def cmd_ask(a) -> None:
    st = {"model": a.model, "role": a.role, "machine": a.machine, "project": a.project, "conv": None}
    print(_show(asyncio.run(_ask(_tools(), a.prompt, st))))


def _show_stories(stories: list[dict], concerns: list[str]) -> None:
    for s_ in stories:
        print(f"  {s_['id']:<8} {s_.get('points', '?'):>3} pts  {s_['title']}" + ("  [human-only step]" if s_.get("human_only") else ""))
        if s_.get("architecture_notes"):
            print(f"           architecture: {str(s_['architecture_notes'])[:150]}")
        for r_ in (s_.get("risks") or [])[:2]:
            print(f"           risk: {str(r_)[:130]}")
        if s_.get("briefs"):
            print(f"           briefs for: {', '.join(s_['briefs'])}")
    for c_ in concerns:
        print(f"  ! concern: {c_}")
    try:
        cap = int((projects_cfg() or {}).get("sprint_capacity_points", 15))
    except (OSError, ValueError, TypeError):
        cap = 15
    m = sf.proposal_summary(stories, cap)
    print(f"\n  AGENT WORK {m['agent_points']:g} pts ({len(m['agent'])} stories): committed {m['committed_points']:g}"
          + (f", stretch {m['stretch_points']:g} ({', '.join(m['stretch'])})" if m["stretch"] else "") + f" · capacity ~{cap}"
          + (f"  ⚠ committed work exceeds capacity by {m['committed_points'] - cap:g} pts: cut or mark stretch" if m["over_capacity"] else "")
          + f"\n  HUMAN STEPS {m['human_points']:g} pts ({len(m['human'])}: {', '.join(m['human']) or '-'}): yours, not planned for agents; see `dis-agent steps`")


def _po_round(a, request: str, previous: list[dict] | None = None, feedback: str = "", previous_concerns: list[str] | None = None) -> list[dict]:
    ctx = Path(a.context_file).read_text() if getattr(a, "context_file", None) else ""
    if not getattr(a, "no_context", False):
        ctx = sf.load_context(viz_repo(), a.project) + ("\n\n" + ctx if ctx else "")
    try:
        capacity = int((projects_cfg() or {}).get("sprint_capacity_points", 15))
    except (OSError, ValueError, TypeError):
        capacity = 15
    stories, done = sf.propose_stories(sc.client(), a.project, a.sprint, request, ctx, a.approved, a.as_agent,
                                       previous=previous, feedback=feedback, previous_concerns=previous_concerns, capacity=capacity)
    d = ROOT / "data" / "scrum" / a.project / a.sprint
    f = d / "stories.json"
    revisions = list(json.loads(f.read_text()).get("revisions", [])) if f.exists() and previous is not None else []
    if feedback:
        revisions.append({"feedback": feedback, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if f.exists():                                                  # never lose an earlier round, revision or not
        (d / f"stories.v{len(list(d.glob('stories.v*.json'))) + 1}.json").write_text(f.read_text())
    (d / ".dismissed").unlink(missing_ok=True)
    p = sf.save(ROOT, a.project, a.sprint, "stories.json", {"project": a.project, "sprint": a.sprint, "request": request,
                "proposed_by": done.get("agent"), "model": done.get("model"), "concerns": done.get("concerns", []),
                "revisions": revisions, "stories": stories})
    print(f"Product Owner ({done.get('agent')} {done.get('model') or ''}) proposed {len(stories)} stories -> {p}")
    _show_stories(stories, done.get("concerns", []))
    return stories


def _default_sprint(a) -> None:
    if not a.sprint:
        a.sprint = next_sprint(a.project)
        print(f"(no -s given: using sprint {a.sprint}, the next free number for {a.project})")


def cmd_po(a) -> None:
    _default_sprint(a)
    f = ROOT / "data" / "scrum" / a.project / a.sprint / "stories.json"
    if a.revise:
        if not f.exists():
            sys.exit(f"{f} not found; run `po` with a request first")
        prev = json.loads(f.read_text())
        last = (prev.get("revisions") or [{}])[-1]
        if not getattr(a, "again", False) and last.get("feedback") == a.revise.strip() and last.get("at"):
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])).total_seconds()
            if age < 3600:
                sys.exit(f"this exact feedback was already applied {int(age // 60)} minutes ago (revision {len(prev['revisions'])}): running it again is another Opus call "
                         "on the already-revised proposal and can change the Product Owner's recommendations. Read the result with "
                         f"`dis-agent next -p {a.project}`, or pass --again if you really want a second pass.")
        _po_round(a, prev["request"], prev["stories"], a.revise.strip(), prev.get("concerns"))
    else:
        if not a.request:
            sys.exit("give the request, or --revise 'feedback' to adjust the existing stories")
        _po_round(a, a.request)
    print(f"\nNothing is queued. Next: dis-agent plan -p {a.project} -s {a.sprint} --story <ID>")


def cmd_planning(a) -> None:
    _default_sprint(a)
    if not sys.stdin.isatty():
        sys.exit("planning is interactive: run it in a terminal (or use `po --revise` for one round)")
    f = ROOT / "data" / "scrum" / a.project / a.sprint / "stories.json"
    if f.exists():
        prev = json.loads(f.read_text())
        stories, request = prev["stories"], prev["request"]
        print(f"Resuming the proposal in {f}\n")
        _show_stories(stories, prev.get("concerns", []))
    else:
        request = input("What should this sprint deliver? ").strip()
        if not request:
            return
        stories = _po_round(a, request)
    rounds = 0
    while True:
        fb = input("\nFeedback for the Product Owner (Enter = accept, q = stop): ").strip()
        if fb.lower() in ("", "q"):
            break
        rounds += 1
        print("  (each round is one Product Owner call; Opus is capped, then falls back to Sonnet)")
        cur = json.loads(f.read_text()) if f.exists() else {}
        stories = _po_round(a, request, stories, fb, cur.get("concerns"))
    print(f"\n{'Accepted' if fb != 'q' else 'Stopped'} after {rounds} revision(s). For each story you want built:\n"
          f"  dis-agent plan -p {a.project} -s {a.sprint} --story <ID>   then   dis-agent approve <plan>")


def cmd_groom(a) -> None:
    ctx = sf.load_context(viz_repo(), a.project)
    rep, done = sf.groom_backlog(sc.client(), a.project, ctx, a.approved, a.as_agent)
    day = datetime.now(timezone.utc).date().isoformat()
    p = sf.save(ROOT, a.project, "grooming", f"groom-{day}.json", rep)
    rev = rep["review"]
    counts = {}
    for r in rev:
        counts[r.get("verdict", "?")] = counts.get(r.get("verdict", "?"), 0) + 1
    print(f"Product Owner ({done.get('agent')} {done.get('model') or ''}) reviewed {len(rev)} open stories: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f"\n-> {p}\n{str(rep.get('summary') or '')[:500]}\n")
    for r in rev:
        if r.get("verdict") != "ok":
            print(f"  {r.get('id'):<8} {r.get('verdict'):<11} " + "; ".join(str(i) for i in (r.get("issues") or [])[:2])[:170])
    for m in rep.get("missing") or []:
        print(f"  missing: {str(m.get('title'))[:90]} — {str(m.get('why'))[:80]}")
    print("\nThis wrote only the report. Nothing in the backlog was changed.")


def cmd_status(a) -> None:
    c = sc.client()
    board = c._call("GET", "/scrum/sprints")
    print(f"{'project':<18}{'closed':>12}  open sprints")
    for p in board["projects"]:
        opens = [f"S{s['sprint']} {s['points_remaining']:g}/{s['points_total']:g}pt" for s in p["sprints"] if s["points_remaining"] > 0]
        comp = "open work only" if p.get("backlog_open_work_only") else f"{p['closed']}/{p['stories']}"
        print(f"{p['project']:<18}{comp:>12}  {', '.join(opens) or '-'}")
    t = board["fleet"]["totals"]
    print(f"\nfleet: {t.get('tasks')} tasks, {t.get('done')} done, agents {t.get('agent_count')}, tokens out {t.get('tokens_out'):,}")
    print("running now:", ", ".join(f"{x['id']} {x.get('role') or x['type']}" for x in board["recent_tasks"] if x["status"] in ("pending", "claimed", "in_progress")) or "nothing")


def cmd_plan(a) -> None:
    f = ROOT / "data" / "scrum" / a.project / a.sprint / "stories.json"
    if not f.exists():
        sys.exit(f"{f} not found; run `dis-agent po` first")
    story = next((s for s in json.loads(f.read_text())["stories"] if s["id"] == a.story), None)
    if not story:
        sys.exit(f"story {a.story} not in {f}")
    if story.get("human_only"):
        sys.exit(f"{a.story} is a HUMAN step, not agent work, so there is nothing to plan. It is on your list: `dis-agent steps -p {a.project} -s {a.sprint}`; "
                 f"when it is done: `dis-agent done -p {a.project} -s {a.sprint} --story {a.story} --evidence \"what you did\"`.")
    cfg = projects_cfg()
    tasks, done = sf.plan_story(sc.client(), a.project, a.sprint, story, sc.role_hints(), a.approved, a.as_agent,
                                machines=cfg["machines"])
    p = sf.save(ROOT, a.project, a.sprint, f"{a.story}.plan.json", {"project": a.project, "sprint": a.sprint,
                "story": story, "planned_by": done.get("agent"), "tasks": tasks})
    print(f"Scrum Master ({done.get('agent')}) planned {len(tasks)} tasks -> {p}\n")
    print(f"{'wave':<5}{'key':<6}{'role':<14}{'agent (default)':<18}{'machine':<14}title")
    pol = sc.role_policy()["roles"]
    for i, layer in enumerate(sf.waves(tasks), 1):
        for t in layer:
            r = pol[t["role"]]
            print(f"{i:<5}{t['key']:<6}{t['role']:<14}{(r.get('agent', '') + ' ' + r.get('model', '')).strip():<18}"
                  f"{(t['machine'] or 'queue picks') + ('!' if t['machine_required'] else ''):<14}{t['title']}")
    print(f"\nRead it, edit the file if you like, then:  dis-agent approve {p}")


def cmd_approve(a) -> None:
    path = Path(a.plan)
    who = os.environ.get("USER", "human")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    plan = sf.approve_plan(path, who, now)
    print(f"approved {path.name} by {plan['approval']['by']} (digest {plan['approval']['digest']}). Editing the tasks voids this.")
    if not plan.get("backlog_id"):
        try:
            r = sf.register_story(viz_repo(), plan["project"], plan["story"], who, now[:10], path.name)
            plan["backlog_id"] = r["id"]
            path.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
            _report(f"{r['id']} registered as planned", r)
        except sf.PlanError as e:
            print(f"  backlog NOT updated: {e}")
    if plan.get("backlog_id") and not plan.get("sprint_number"):
        try:
            n = _sprint_number(plan["project"], plan["sprint"])
            req = (json.loads((path.parent / "stories.json").read_text()).get("request") if (path.parent / "stories.json").exists() else "") or plan["story"]["title"]
            if _sprint_step(f"sprint-{n:02d}.md now holds {plan['backlog_id']}", lambda: sf.sprint_open_story(
                    viz_repo(), plan["project"], n, plan["story"], plan["backlog_id"], req, now[:10])):
                plan["sprint_number"] = n                          # only once it worked, so a later approve retries a failure
                path.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
        except sf.PlanError as e:
            print(f"  sprint file NOT updated: {e}")
    print(f"Run it:  dis-agent go {path}")


def cmd_go(a) -> None:
    path = Path(a.plan)
    plan = json.loads(path.read_text())
    cfg = projects_cfg()
    tasks = sf.parse_plan(json.dumps({"tasks": plan["tasks"]}), set(sc.role_hints()), set(cfg["machines"]))
    ok = sf.is_approved(plan)
    sd = path.parent
    if (sd / "stories.json").exists():
        blockers = sf.blocking_steps(plan["story"], json.loads((sd / "stories.json").read_text())["stories"], set(_steps_done(sd)))
        if blockers:
            print(f"  ⚠ this story depends on YOUR step(s) {', '.join(blockers)}, not marked done (`dis-agent steps -p {plan['project']} -s {plan['sprint']}`). "
                  "Agents will do their part, but check the dependency note before anything is deployed.")
    if not ok:
        print("NOT APPROVED — showing a dry run. " + ("The plan changed since you approved it." if plan.get("approval") else f"Approve first: dis-agent approve {path}"))
    bid = plan.get("backlog_id")
    if ok and bid:
        try:
            _report(f"{bid} in progress", sf.mark_story(viz_repo(), plan["project"], bid, "🏃 in progress", None))
        except sf.PlanError as e:
            print(f"  backlog NOT updated: {e}")
        if plan.get("sprint_number"):
            _sprint_step(f"sprint-{plan['sprint_number']:02d}.md: {bid} in progress", lambda: sf.sprint_mark(
                viz_repo(), plan["project"], plan["sprint_number"], bid, "🏃 in progress", datetime.now(timezone.utc).date().isoformat()))
    log = sf.dispatch(sc.client(), plan["project"], plan["sprint"], plan["story"], tasks, go=ok,
                      approved=a.approved, cwd=a.cwd, project_cfg=(cfg["projects"] or {}).get(plan["project"]))
    if ok:
        out = path.with_suffix(".run.json")
        out.write_text(json.dumps(log, indent=2))
        rows = sc.client()._call("GET", "/tasks", params={"limit": 500})
        ev = sf.story_evidence(rows, plan["project"], plan["sprint"], plan["story"]["id"])
        if bid:
            try:
                _report(f"{bid} in review", sf.mark_story(viz_repo(), plan["project"], bid, "🔍 in review",
                        "work finished; PRs " + (", ".join(ev["pull_requests"]) or "none found")
                        + "; validation: " + sf.validation_summary(ev.get("validation")) + "; awaiting sprint review"))
            except sf.PlanError as e:
                print(f"  backlog NOT updated: {e}")
            if plan.get("sprint_number"):
                _sprint_step(f"sprint-{plan['sprint_number']:02d}.md: {bid} in review", lambda: sf.sprint_mark(
                    viz_repo(), plan["project"], plan["sprint_number"], bid, "🔍 in review", datetime.now(timezone.utc).date().isoformat()))
        print(f"\nrun log -> {out}\nNext: dis-agent review -p {plan['project']} -s {plan['sprint']}, then dis-agent close ...")


_BADGE = {"in_progress": "🏃 in progress", "in_review": "🔍 in review", "blocked": "🚧 blocked", "planned": "🔵 planned",
          "done": "✅ done", "superseded": "⛔ superseded", "cancelled": "❌ cancelled"}


def _story_line(x: dict, width: int = 78) -> str:
    pts = f"{x['points']:g}" if x.get("points") else ""
    return f"    {x['id'] or '-':<8} {_BADGE.get(x['status'], x['status']):<14} {pts:>3}  {str(x['title'])[:width]}"


def cmd_backlog(a) -> None:
    b = _board()
    p = next((x for x in b["projects"] if x["project"] == a.project), None)
    if not p:
        sys.exit(f"unknown project {a.project!r}; known: {', '.join(x['project'] for x in b['projects'])}")
    v = p["backlog_view"]
    c = v["counts"]
    print(f"{a.project}: {p['closed']} closed · {c.get('in_progress', 0)} in progress · {c.get('in_review', 0)} in review · "
          f"{c.get('blocked', 0)} blocked · {c.get('planned', 0)} planned" + (f" · {c.get('cancelled', 0)} cancelled" if c.get("cancelled") else ""))
    for sp in v["sprints"]:
        if not sp["stories"] and not a.all:
            continue
        print(f"\n  Sprint {sp['sprint']}  {sp.get('points_done') or 0:g}/{sp.get('points_total') or 0:g} pts"
              + (f"  started {sp['started']}" if sp.get("started") else "") + (f"  — {str(sp['goal'])[:80]}" if sp.get("goal") else ""))
        for x in sp["stories"]:
            print(_story_line(x))
            if x.get("reason") and x["status"] in ("blocked", "cancelled"):
                print(f"{'':<27}why: {x['reason'][:150]}")
    print(f"\n  Not in a sprint yet ({len(v['unscheduled'])} open; {v['closed_unscheduled']} closed ones not shown)")
    for x in v["unscheduled"][: a.limit]:
        print(_story_line(x))
        if x["status"] == "blocked":
            print(f"{'':<27}why: {x['evidence'][:150] or 'no reason recorded in the backlog'}")
    if len(v["unscheduled"]) > a.limit:
        print(f"    ... {len(v['unscheduled']) - a.limit} more (--limit)")
    if v["cancelled"]:
        print(f"\n  Cancelled in the last 60 days ({v['cancelled_hidden']} older hidden)")
        for x in v["cancelled"]:
            print(_story_line(x) + (f"   [{x['evidence'][:80]}]" if x.get("evidence") else ""))


def cmd_cancel(a) -> None:
    who = os.environ.get("USER", "human")
    if not a.reason or not a.reason.strip():
        sys.exit("a cancellation needs --reason")
    if a.yes:
        ok = True
    elif sys.stdin.isatty():
        ok = input(f"Cancel {a.story} in {a.project}? It stays visible for 60 days, then drops off the board. [y/N] ").strip().lower() == "y"
    else:
        sys.exit("not cancelling: needs your confirmation (run this in a terminal, or pass --yes)")
    if not ok:
        print("left as it was")
        return
    try:
        day = datetime.now(timezone.utc).date().isoformat()
        _report(f"{a.story} CANCELLED", sf.cancel_story(viz_repo(), a.project, a.story, who, day, a.reason))
    except sf.PlanError as e:
        sys.exit(f"not cancelled: {e}")
    n = sf.find_sprint_of(viz_repo(), a.project, a.story)
    if n:
        _sprint_step(f"sprint-{n:02d}.md: {a.story} cancelled, out of scope", lambda: sf.sprint_mark(
            viz_repo(), a.project, n, a.story, f"❌ cancelled {day}", day, closing=True))


def _local_plans() -> list[dict]:
    from shared import scrum_flow
    out = []
    for f in sorted((ROOT / "data" / "scrum").glob("*/*/*.plan.json")):
        try:
            pl = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        out.append({"project": pl["project"], "sprint": pl["sprint"], "sprint_number": pl.get("sprint_number"), "story_id": pl["story"]["id"],
                    "backlog_id": pl.get("backlog_id"), "title": pl["story"]["title"], "approved": scrum_flow.is_approved(pl),
                    "ran": f.with_suffix(".run.json").exists(), "path": str(f.relative_to(ROOT))})
    return out


def _local_proposals() -> dict[str, dict]:
    """Sprint proposals waiting for feedback or planning: stories.json whose stories are not all planned yet."""
    out: dict[str, dict] = {}
    for f in sorted((ROOT / "data" / "scrum").glob("*/*/stories.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if (f.parent / ".dismissed").exists():
            continue
        planned = {json.loads(p.read_text())["story"]["id"] for p in f.parent.glob("*.plan.json") if p.read_text().strip()}
        todo = [x for x in d.get("stories", []) if x["id"] not in planned and not x.get("human_only")]
        if todo:
            out[d["project"]] = {"sprint": d["sprint"], "stories": len(d["stories"]), "points": sum(x.get("points") or 0 for x in d["stories"]),
                                 "todo": [x["id"] for x in todo], "planned": len(planned)}
    return out


def _steps_done(d: Path) -> dict:
    f = d / "human-steps.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _local_human_steps() -> dict[str, list[dict]]:
    """Pending human steps of waiting proposals, with the agent stories each one blocks."""
    out: dict[str, list[dict]] = {}
    for f in sorted((ROOT / "data" / "scrum").glob("*/*/stories.json")):
        if (f.parent / ".dismissed").exists():
            continue
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        done = set(_steps_done(f.parent))
        for st in d.get("stories", []):
            if st.get("human_only") and st["id"] not in done:
                blocks = [o["id"] for o in d["stories"] if not o.get("human_only") and st["id"] in sf.blocking_steps(o, d["stories"], done)]
                out.setdefault(d["project"], []).append({"id": st["id"], "title": st["title"], "sprint": d["sprint"], "blocks": blocks})
    return out


def cmd_steps(a) -> None:
    sprint = resolve_sprint_dir(a.project, a.sprint)
    d = ROOT / "data" / "scrum" / a.project / sprint
    if not (d / "stories.json").exists():
        sys.exit(f"no proposal at {d}")
    stories = json.loads((d / "stories.json").read_text())["stories"]
    done = _steps_done(d)
    steps = [s for s in stories if s.get("human_only")]
    if not steps:
        print("This proposal has no human steps.")
        return
    print(f"Your steps for {a.project} sprint {sprint} (agents never do these):\n")
    for st in steps:
        mark = f"DONE {done[st['id']]['done_at'][:10]}" if st["id"] in done else "TODO"
        blocks = [o["id"] for o in stories if not o.get("human_only") and st["id"] in sf.blocking_steps(o, stories, set(done))]
        print(f"  [{mark}] {st['id']}  {st['title']}  ({st.get('points') or 0:g} pt)" + (f"   blocks: {', '.join(blocks)}" if blocks and st["id"] not in done else ""))
        for c in st.get("acceptance", []):
            print(f"           - {c}")
        if st["id"] in done:
            print(f"           evidence: {done[st['id']]['evidence']}")
    todo = [s["id"] for s in steps if s["id"] not in done]
    if todo:
        print(f"\nWhen one is finished: dis-agent done -p {a.project} -s {sprint} --story <ID> --evidence \"what you did and how you checked it\"")


def cmd_done(a) -> None:
    sprint = resolve_sprint_dir(a.project, a.sprint)
    d = ROOT / "data" / "scrum" / a.project / sprint
    stories = json.loads((d / "stories.json").read_text())["stories"] if (d / "stories.json").exists() else []
    st = next((s for s in stories if s["id"] == a.story), None)
    if not st or not st.get("human_only"):
        sys.exit(f"{a.story} is not a human step of this proposal (`dis-agent steps -p {a.project} -s {sprint}` lists them)")
    if not (a.evidence or "").strip():
        sys.exit("a step is done with evidence: --evidence \"what you did and how you checked it\" (never put a secret in it)")
    if a.yes:
        ok = True
    elif sys.stdin.isatty():
        ok = input(f"Mark {a.story} done: {st['title'][:70]} ? [y/N] ").strip().lower() == "y"
    else:
        sys.exit("not marking it done: needs your confirmation (run this in a terminal, or pass --yes)")
    if not ok:
        print("left as TODO")
        return
    done = _steps_done(d)
    done[a.story] = {"done_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": os.environ.get("USER", "human"), "evidence": a.evidence.strip()}
    (d / "human-steps.json").write_text(json.dumps(done, indent=2, ensure_ascii=False))
    unblocked = [o["id"] for o in stories if not o.get("human_only") and a.story in re.findall(r"\bS-\d+\b", " ".join(map(str, o.get("depends_on") or [])))]
    print(f"{a.story} marked done." + (f" Now unblocked (this step was one of their dependencies): {', '.join(unblocked)}" if unblocked else ""))


def cmd_dismiss(a) -> None:
    """Stop `next` offering a sprint proposal you will not pursue (the files are kept; a new `po` round revives it)."""
    sprint = resolve_sprint_dir(a.project, a.sprint)
    d = ROOT / "data" / "scrum" / a.project / sprint
    if not (d / "stories.json").exists():
        sys.exit(f"no proposal at {d}")
    (d / ".dismissed").write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print(f"dismissed the {a.project} sprint {sprint} proposal; its files are kept in {d}")


def resolve_sprint_dir(project: str, s: str) -> str:
    base = ROOT / "data" / "scrum" / project
    if (base / s).is_dir():
        return s
    for label, n in _sprint_maps(project).items():
        if str(n) == s.lstrip("0") and (base / label).is_dir():
            return label
    sys.exit(f"no sprint {s!r} for {project} under {base}")


def cmd_next(a) -> None:
    from shared import scrum_next
    groomed = {}
    for g in sorted((ROOT / "data" / "scrum").glob("*/grooming/groom-*.json")):
        groomed[g.parent.parent.name] = g.stem.removeprefix("groom-")
    sugs = scrum_next.suggest(_board(), _local_plans(), groomed, a.project, a.limit, proposals=_local_proposals(), steps=_local_human_steps())
    if a.json:
        print(json.dumps(sugs, indent=2, ensure_ascii=False))
        return
    print(scrum_next.render(sugs))


def _board() -> dict:
    b = sc.client()._call("GET", "/scrum/sprints")
    if "digest" not in b:
        sys.exit("this orchestrator is too old to build the scrum digest: deploy the latest distributed-infra to it")
    return b


def cmd_scrum(a) -> None:
    from shared import scrum_digest
    b = _board()
    t = b["fleet"]["totals"]
    fleet = (f"Fleet: {t.get('tasks')} tasks, {t.get('done')} done; tokens out {t.get('tokens_out'):,}."
             + (f" Opus this month ${b['digest']['opus']['spent']:.2f} of ${b['digest']['opus']['cap']:.0f}." if b["digest"].get("opus") else ""))
    md = scrum_digest.to_markdown(b["digest"], fleet)
    if a.narrate:
        t_ = sc.client().queue(sf.tags("scrum-of-scrums", b["digest"]["as_of"], None, "scrum_master", False, a.as_agent, None,
                                       prompt="You are the Scrum Master running the scrum of scrums for the human product owner. From this digest write a "
                                              "tight briefing (at most 12 lines): the 3 things that most need his attention, the risks you see, and the "
                                              "adjustments you recommend, each with the reason. Do not invent facts that are not in the digest.\n\n" + md,
                                       timeout=600), "scrum of scrums")
        done = sc.client().wait([t_["id"]])[t_["id"]]
        md += "\n## Scrum Master's briefing\n\n" + (sf.answer(done) if done["status"] == "done" else f"(the briefing task ended {done['status']})") + "\n"
    d = ROOT / "data" / "scrum" / "_all"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{b['digest']['as_of']}.md").write_text(md)
    print(md)
    print(f"(saved {d / (b['digest']['as_of'] + '.md')})\nReview what is waiting:  dis-agent review")


def cmd_review_queue(a) -> None:
    from shared import scrum_next
    items = scrum_next.review_items(_board(), _local_plans(), (projects_cfg().get("projects") or {}), a.project)
    if not items:
        print("Nothing is waiting for your review.")
        return
    print(f"{len(items)} stories wait for your sprint review:\n")
    for r in items:
        print(f"  {r['project']} {r['id']}  {r['title'][:86]}" + (f"   [sprint {r['sprint']}]" if r["sprint"] else "   [not in a sprint]"))
        if r["evidence"]:
            print(f"      evidence: {r['evidence'][:230]}")
        if r["prs"]:
            print(f"      PRs:      {', '.join(r['prs'])}")
        print(f"      confirm:  {r['command']}")
    print("\nA story the fleet built has validation and review evidence (`/close` shows it). Any other story is confirmed with `dis-agent accept`,\n"
          "which closes it only on your say-so and, when you give a PR, only after GitHub confirms it is merged. Cancel one with `dis-agent cancel`.")


def cmd_accept(a) -> None:
    """Confirm, at a sprint review, a story the fleet did not build. Evidence is required: a merged PR or what you verified."""
    who = os.environ.get("USER", "human")
    day = datetime.now(timezone.utc).date().isoformat()
    if not a.pr and not (a.evidence or "").strip():
        sys.exit("accepting needs evidence: --pr <url of a MERGED pull request> or --evidence \"what you verified\"")
    parts = []
    if a.pr:
        try:
            m = sf.verify_pr_merged(a.pr)
        except sf.PlanError as e:
            sys.exit(f"not accepted: {e}")
        parts.append(f"{a.pr} merged {m['merged_at']}")
        print(f"  GitHub confirms: {a.pr} is merged ({m['merged_at']}): {m['title'][:80]}")
    if (a.evidence or "").strip():
        parts.append(a.evidence.strip())
    if a.yes:
        ok = True
    elif sys.stdin.isatty():
        ok = input(f"Accept {a.story} in {a.project} as done? You confirm the sprint review accepts it [y/N] ").strip().lower() == "y"
    else:
        sys.exit("not accepting: needs your confirmation (run this in a terminal, or pass --yes)")
    if not ok:
        print("left in review")
        return
    try:
        _report(f"{a.story} ACCEPTED", sf.close_story(viz_repo(), a.project, a.story, who, day, "; ".join(parts)))
    except sf.PlanError as e:
        sys.exit(f"not accepted: {e}")
    n = sf.find_sprint_of(viz_repo(), a.project, a.story)
    if n:
        _sprint_step(f"sprint-{n:02d}.md: {a.story} done, burndown updated", lambda: sf.sprint_mark(
            viz_repo(), a.project, n, a.story, f"✅ done {day}", day, closing=True))


def cmd_review(a) -> None:
    if not a.project or not a.sprint:
        return cmd_review_queue(a)
    a.sprint = resolve_sprint(a.project, a.sprint)
    c = sc.client()
    rows = c._call("GET", "/tasks", params={"limit": 500})
    board = c._call("GET", "/scrum/sprints")
    bs = next((s for p in board["projects"] if p["project"] == a.project for s in p["sprints"] if str(s["sprint"]) == a.sprint), None)
    data, md = sf.sprint_review(rows, a.project, a.sprint, bs)
    d = ROOT / "data" / "scrum" / a.project / a.sprint
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.md").write_text(md)
    print(md + f"\n(saved {d / 'review.md'})")


def cmd_validate(a) -> None:
    sprint = resolve_sprint(a.project, a.sprint)
    base = ROOT / "data" / "scrum" / a.project / sprint
    plan = next((json.loads(f.read_text()) for f in sorted(base.glob("*.plan.json"))
                 if a.story in (json.loads(f.read_text()).get("backlog_id"), json.loads(f.read_text())["story"]["id"])), None)
    if not plan:
        sys.exit(f"no plan for {a.story} under {base}")
    cfg = projects_cfg()
    pre = sf.repo_preamble(a.project, (cfg["projects"] or {}).get(a.project), plan["story"]["id"])
    c = sc.client()
    t = c.queue(sf.tags(a.project, sprint, plan["story"]["id"], "tester", a.approved, None, None,
                        prompt=pre + sf.validate_prompt(plan["story"], a.branch), timeout=1800, flow_key="validate"), f"validate {plan['story']['id']}")
    print(f"tester queued ({t['id'][:8]}); this takes a few minutes ...")
    done = c.wait([t["id"]])[t["id"]]
    if done["status"] != "done":
        sys.exit(f"the tester task ended {done['status']}: {done.get('notes') or ''}")
    v = sf.parse_validation(sf.answer(done), len(plan["story"].get("acceptance", [])) or None)
    (base / f"validation-{plan['story']['id']}.json").write_text(json.dumps(v, indent=2))
    print(f"validation by {done.get('agent')}: {sf.validation_summary(v)}")
    for cr in v["criteria"]:
        print(f"  {cr['n']}. {cr['status']:<10} {cr['evidence'][:130]}")
    if v["passed"] is not True:
        print("\nNot a pass: dis-agent close will refuse. Read the evidence above; --force on close is yours to use if you accept it.")


def cmd_close(a) -> None:
    a.sprint = resolve_sprint(a.project, a.sprint)
    base = ROOT / "data" / "scrum" / a.project / a.sprint
    plans = {}
    for f in sorted(base.glob("*.plan.json")):
        pl = json.loads(f.read_text())
        if pl.get("backlog_id"):
            plans[pl["story"]["id"]] = pl
    by_backlog = {pl["backlog_id"]: sid for sid, pl in plans.items()}
    want = [by_backlog.get(x, x) for x in a.story] if a.story else list(plans)       # --story takes the plan's ID or the backlog's
    if not want:
        sys.exit(f"no registered stories under {base}; approve a plan first")
    rows = sc.client()._call("GET", "/tasks", params={"limit": 500})
    who = os.environ.get("USER", "human")
    for sid in want:
        pl = plans.get(sid)
        if not pl:
            print(f"{sid}: no registered plan, skipping")
            continue
        ev, bid = sf.story_evidence(rows, a.project, a.sprint, sid), pl["backlog_id"]
        print(f"\n{bid} ({sid}) {pl['story']['title']}\n  tasks {ev['done']}/{ev['tasks']} done, agents {', '.join(ev['agents']) or '-'}, "
              f"reviewed: {'yes' if ev['reviewed'] else 'NO'}\n  PRs: {', '.join(ev['pull_requests']) or 'none found'}"
              + (f"\n  stuck: {', '.join(ev['stuck'])}" if ev["stuck"] else ""))
        v = ev.get("validation")
        print("  validation: " + sf.validation_summary(v))
        for c in (v or {}).get("criteria", []):
            print(f"    {c['n']}. {c['status']:<10} {c['evidence'][:110]}")
        if ev["review_excerpt"]:
            print("  review says: " + ev["review_excerpt"].replace("\n", " ")[:400])
        problems = [m for m, bad in (("not every task is done", ev["done"] < ev["tasks"] or ev["tasks"] == 0), ("no review ran", not ev["reviewed"]),
                                     ("validation did not pass: " + sf.validation_summary(v), not (v and v.get("passed") is True))) if bad]
        if problems and not a.force:
            print(f"  NOT closing: {', '.join(problems)} (use --force only if you have reviewed it yourself)")
            continue
        if getattr(a, "check", False):
            print("  CHECK ONLY: every gate passes; run again with --yes (or answer y in a terminal) to close it")
            continue
        if a.yes:
            ok = True
        elif sys.stdin.isatty():
            ok = input(f"  Close {bid}? You confirm the sprint review accepts it [y/N] ").strip().lower() == "y"
        else:
            print("  not closing: needs your confirmation (run this in a terminal, or pass --yes)")
            continue
        if ok:
            try:
                r = sf.close_story(viz_repo(), a.project, bid, who, datetime.now(timezone.utc).date().isoformat(),
                                   "PRs " + (", ".join(ev["pull_requests"]) or "none"))
                _report(f"{bid} CLOSED", r)
                day = datetime.now(timezone.utc).date().isoformat()
                if pl.get("sprint_number"):
                    _sprint_step(f"sprint-{pl['sprint_number']:02d}.md: {bid} done, burndown updated", lambda: sf.sprint_mark(
                        viz_repo(), a.project, pl["sprint_number"], bid, f"✅ done {day}", day, closing=True))
            except sf.PlanError as e:
                print(f"  not closed: {e}")
        else:
            print("  left in review")


def main() -> None:
    ap = argparse.ArgumentParser(prog="dis-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    ch = sub.add_parser("chat"); ch.set_defaults(fn=cmd_chat)
    ch.add_argument("--model", default="auto", choices=list(MODELS)); ch.add_argument("-p", "--project")
    q = sub.add_parser("ask"); q.set_defaults(fn=cmd_ask)
    q.add_argument("--model", default="auto", choices=list(MODELS)); q.add_argument("--role"); q.add_argument("--machine")
    q.add_argument("-p", "--project"); q.add_argument("prompt")
    sub.add_parser("roles").set_defaults(fn=sc.cmd_roles)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="as_agent"); common.add_argument("--approved", action="store_true")
    po = sub.add_parser("po", parents=[common]); po.set_defaults(fn=cmd_po)
    po.add_argument("-p", "--project", required=True); po.add_argument("-s", "--sprint", help="sprint label or number (default: the next free number)")
    po.add_argument("--context-file"); po.add_argument("--no-context", action="store_true", help="skip the architecture + backlog context")
    po.add_argument("--revise", metavar="FEEDBACK", help="adjust the existing stories with your planning feedback")
    po.add_argument("--again", action="store_true", help="apply the same feedback again (normally refused within an hour)")
    po.add_argument("request", nargs="?")
    pn = sub.add_parser("planning", parents=[common]); pn.set_defaults(fn=cmd_planning)
    pn.add_argument("-p", "--project", required=True); pn.add_argument("-s", "--sprint", help="sprint label or number (default: the next free number)")
    pn.add_argument("--context-file"); pn.add_argument("--no-context", action="store_true")
    gm = sub.add_parser("groom", parents=[common]); gm.set_defaults(fn=cmd_groom); gm.add_argument("-p", "--project", required=True)
    pl = sub.add_parser("plan", parents=[common]); pl.set_defaults(fn=cmd_plan)
    pl.add_argument("-p", "--project", required=True); pl.add_argument("-s", "--sprint", required=True); pl.add_argument("--story", required=True)
    ap_ = sub.add_parser("approve"); ap_.set_defaults(fn=cmd_approve); ap_.add_argument("plan")
    go = sub.add_parser("go", parents=[common]); go.set_defaults(fn=cmd_go); go.add_argument("plan"); go.add_argument("--cwd")
    cl = sub.add_parser("close"); cl.set_defaults(fn=cmd_close)
    cl.add_argument("-p", "--project", required=True); cl.add_argument("-s", "--sprint", required=True)
    cl.add_argument("--story", action="append"); cl.add_argument("--yes", action="store_true", help="you have read the review and confirm")
    cl.add_argument("--force", action="store_true")
    cl.add_argument("--check", action="store_true", help="show the evidence and whether it would close; never closes and never prompts")
    vl = sub.add_parser("validate", parents=[common]); vl.set_defaults(fn=cmd_validate)
    vl.add_argument("-p", "--project", required=True); vl.add_argument("-s", "--sprint", required=True)
    vl.add_argument("--story", required=True, help="the plan's story id (S-1) or its backlog id (DI-056)")
    vl.add_argument("--branch", help="for a story already merged: test this branch (usually main); new tests go on a separate PR branch")
    ds = sub.add_parser("dismiss"); ds.set_defaults(fn=cmd_dismiss)
    ds.add_argument("-p", "--project", required=True); ds.add_argument("-s", "--sprint", required=True)
    stp = sub.add_parser("steps"); stp.set_defaults(fn=cmd_steps)
    stp.add_argument("-p", "--project", required=True); stp.add_argument("-s", "--sprint", required=True)
    dn = sub.add_parser("done"); dn.set_defaults(fn=cmd_done)
    dn.add_argument("-p", "--project", required=True); dn.add_argument("-s", "--sprint", required=True); dn.add_argument("--story", required=True)
    dn.add_argument("--evidence"); dn.add_argument("--yes", action="store_true")
    nx = sub.add_parser("next"); nx.set_defaults(fn=cmd_next); nx.add_argument("-p", "--project")
    nx.add_argument("--limit", type=int, default=6); nx.add_argument("--json", action="store_true")
    bk = sub.add_parser("backlog"); bk.set_defaults(fn=cmd_backlog)
    bk.add_argument("-p", "--project", required=True); bk.add_argument("--limit", type=int, default=40)
    bk.add_argument("--all", action="store_true", help="also list sprints with no stories")
    ac = sub.add_parser("accept"); ac.set_defaults(fn=cmd_accept)
    ac.add_argument("-p", "--project", required=True); ac.add_argument("--story", required=True)
    ac.add_argument("--pr", help="URL of a pull request that must be MERGED (checked with GitHub)")
    ac.add_argument("--evidence", help="what you verified, when there is no PR"); ac.add_argument("--yes", action="store_true")
    cn = sub.add_parser("cancel"); cn.set_defaults(fn=cmd_cancel)
    cn.add_argument("-p", "--project", required=True); cn.add_argument("--story", required=True)
    cn.add_argument("--reason", required=True); cn.add_argument("--yes", action="store_true")
    sm = sub.add_parser("scrum"); sm.set_defaults(fn=cmd_scrum); sm.add_argument("--narrate", action="store_true", help="have the Scrum Master (Sonnet) write the briefing")
    sm.add_argument("--as", dest="as_agent")
    rv = sub.add_parser("review"); rv.set_defaults(fn=cmd_review)
    rv.add_argument("-p", "--project"); rv.add_argument("-s", "--sprint")
    a = ap.parse_args()
    if not a.cmd:
        a = ap.parse_args(["chat"])
    try:
        a.fn(a)
    except sf.PlanError as e:
        sys.exit(f"stopped: {e}")


if __name__ == "__main__":
    main()
