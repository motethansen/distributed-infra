#!/usr/bin/env python3
"""dis-agent — one command for the distributed agents.

  dis-agent                       chat (switch LLM with /model, /role, /machine; switching keeps the conversation)
  dis-agent ask "question"        one question, one answer
  dis-agent roles                 who holds which role + live usage
  dis-agent status                open sprints, remaining points, what the fleet is doing
  dis-agent po   -p PROJ -s SPR "request"         Product Owner proposes stories (nothing queued)
  dis-agent plan -p PROJ -s SPR --story ID        Scrum Master plans roles, machines, order (nothing queued)
  dis-agent approve PLAN                          you approve exactly that plan
  dis-agent go PLAN                               run an approved plan (dry run unless approved)
  dis-agent review -p PROJ -s SPR                 sprint review: what ran, PRs to look at, what is stuck

Everything it queues carries project/sprint/story/role, so Vantage's Scrum page shows who did what.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
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
    plan = sf.approve_plan(path, os.environ.get("USER", "human"), datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print(f"approved {path.name} by {plan['approval']['by']} (digest {plan['approval']['digest']}). Editing the tasks voids this.\n"
          f"Run it:  dis-agent go {path}")


def cmd_go(a) -> None:
    path = Path(a.plan)
    plan = json.loads(path.read_text())
    cfg = projects_cfg()
    tasks = sf.parse_plan(json.dumps({"tasks": plan["tasks"]}), set(sc.role_hints()), set(cfg["machines"]))
    ok = sf.is_approved(plan)
    if not ok:
        print("NOT APPROVED — showing a dry run. " + ("The plan changed since you approved it." if plan.get("approval") else f"Approve first: dis-agent approve {path}"))
    log = sf.dispatch(sc.client(), plan["project"], plan["sprint"], plan["story"], tasks, go=ok,
                      approved=a.approved, cwd=a.cwd, project_cfg=(cfg["projects"] or {}).get(plan["project"]))
    if ok:
        out = path.with_suffix(".run.json")
        out.write_text(json.dumps(log, indent=2))
        print(f"\nrun log -> {out}\nNext: dis-agent review -p {plan['project']} -s {plan['sprint']}")


def cmd_review(a) -> None:
    c = sc.client()
    rows = c._call("GET", "/tasks", params={"limit": 500})
    board = c._call("GET", "/scrum/sprints")
    bs = next((s for p in board["projects"] if p["project"] == a.project for s in p["sprints"] if str(s["sprint"]) == a.sprint), None)
    data, md = sf.sprint_review(rows, a.project, a.sprint, bs)
    d = ROOT / "data" / "scrum" / a.project / a.sprint
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.md").write_text(md)
    print(md + f"\n(saved {d / 'review.md'})")


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
    po = sub.add_parser("po", parents=[common]); po.set_defaults(fn=sc.cmd_po)
    po.add_argument("-p", "--project", required=True); po.add_argument("-s", "--sprint", required=True)
    po.add_argument("--context-file"); po.add_argument("request")
    pl = sub.add_parser("plan", parents=[common]); pl.set_defaults(fn=cmd_plan)
    pl.add_argument("-p", "--project", required=True); pl.add_argument("-s", "--sprint", required=True); pl.add_argument("--story", required=True)
    ap_ = sub.add_parser("approve"); ap_.set_defaults(fn=cmd_approve); ap_.add_argument("plan")
    go = sub.add_parser("go", parents=[common]); go.set_defaults(fn=cmd_go); go.add_argument("plan"); go.add_argument("--cwd")
    rv = sub.add_parser("review"); rv.set_defaults(fn=cmd_review)
    rv.add_argument("-p", "--project", required=True); rv.add_argument("-s", "--sprint", required=True)
    a = ap.parse_args()
    if not a.cmd:
        a = ap.parse_args(["chat"])
    try:
        a.fn(a)
    except sf.PlanError as e:
        sys.exit(f"stopped: {e}")


if __name__ == "__main__":
    main()
