#!/usr/bin/env python3
"""Scrum flow CLI: ask the Product Owner and Scrum Master to assign roles, then dispatch.

  scrum_cli.py roles                                        who holds which role, live usage per role
  scrum_cli.py po   -p budgetapp -s S6 "request..."         Product Owner proposes stories (no queueing)
  scrum_cli.py plan -p budgetapp -s S6 --story BA-201       Scrum Master breaks one story into role-tagged tasks
  scrum_cli.py dispatch --plan data/scrum/.../BA-201.plan.json [--go]   queue the tasks (dry run without --go)

Flags: --as agy|codex|claude|deepseek hands the PO/SM role to another agent (e.g. Claude near its limit);
--approved is required for an isolated project (urbanlife); --cwd is the repo path on the worker.
Proposal and plan files go to data/scrum/<project>/<sprint>/ — read them before dispatching.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from shared import scrum_flow as sf  # noqa: E402


def _env() -> dict:
    env = dict(os.environ)
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def client() -> sf.FleetClient:
    e = _env()
    urls = [u.strip() for u in e.get("ORCHESTRATOR_URLS", "http://100.76.214.54:8000,http://100.112.241.6:8000").split(",") if u.strip()]
    if not e.get("SECRET_KEY"):
        sys.exit("SECRET_KEY not set (env or .env)")
    return sf.FleetClient(urls, e["SECRET_KEY"], client="scrum-cli@" + os.uname().nodename.split(".")[0])


def role_policy() -> dict:
    return yaml.safe_load((ROOT / "config" / "roles.yaml").read_text())


def role_hints() -> dict[str, str]:
    return {n: s.get("best_for", "") for n, s in role_policy()["roles"].items()}


def cmd_roles(a) -> None:
    pol = role_policy()
    live = {}
    try:
        live = {g["key"]: g for g in client().usage("role")["groups"]}
    except Exception as e:                                       # board data is a nicety, not required
        print(f"(live usage unavailable: {e})")
    print(f"{'role':<15} {'default agent':<24} {'tasks':>6} {'tok out':>9}  best for")
    for n, s in pol["roles"].items():
        who = (s.get("agent", "claude") + (" " + s["model"] if s.get("model") else "")).strip()
        g = live.get(n, {})
        print(f"{n:<15} {who:<24} {g.get('tasks', 0):>6} {g.get('tokens_out', 0):>9}  {s.get('best_for', '')}")
    print("\nDeepSeek runs for:", ", ".join(pol.get("deepseek_projects") or []) or "no project")
    print("Isolated (need --approved):", ", ".join(pol.get("isolated_projects") or []) or "none")


def cmd_po(a) -> None:
    ctx = Path(a.context_file).read_text() if a.context_file else ""
    stories, done = sf.propose_stories(client(), a.project, a.sprint, a.request, ctx, a.approved, a.as_agent)
    p = sf.save(ROOT, a.project, a.sprint, "stories.json", {"project": a.project, "sprint": a.sprint,
                "request": a.request, "proposed_by": done.get("agent"), "stories": stories})
    print(f"Product Owner ({done.get('agent')} {done.get('model') or ''}) proposed {len(stories)} stories -> {p}")
    for s in stories:
        print(f"  {s['id']:<8} {s.get('points', '?'):>3} pts  {s['title']}" + ("  [human-only step]" if s.get("human_only") else ""))
    print("\nNothing is queued. Edit/approve the file, then: scrum_cli.py plan -p {} -s {} --story <ID>".format(a.project, a.sprint))


def cmd_plan(a) -> None:
    f = ROOT / "data" / "scrum" / a.project / a.sprint / "stories.json"
    if not f.exists():
        sys.exit(f"{f} not found; run `po` first")
    story = next((s for s in json.loads(f.read_text())["stories"] if s["id"] == a.story), None)
    if not story:
        sys.exit(f"story {a.story} not in {f}")
    tasks, done = sf.plan_story(client(), a.project, a.sprint, story, role_hints(), a.approved, a.as_agent)
    p = sf.save(ROOT, a.project, a.sprint, f"{a.story}.plan.json", {"project": a.project, "sprint": a.sprint,
                "story": story, "planned_by": done.get("agent"), "tasks": tasks})
    print(f"Scrum Master ({done.get('agent')}) planned {len(tasks)} tasks -> {p}")
    for i, layer in enumerate(sf.waves(tasks), 1):
        for t in layer:
            print(f"  wave {i}  {t['key']:<6} {t['role']:<13} {t['title']}")
    print(f"\nDry run first: scrum_cli.py dispatch --plan {p}   (add --go to queue)")


def cmd_dispatch(a) -> None:
    plan = json.loads(Path(a.plan).read_text())
    tasks = sf.parse_plan(json.dumps({"tasks": plan["tasks"]}), set(role_hints()))      # re-validate the file as edited
    log = sf.dispatch(client(), plan["project"], plan["sprint"], plan["story"], tasks, go=a.go,
                      approved=a.approved, cwd=a.cwd)
    if a.go:
        out = Path(a.plan).with_suffix(".run.json")
        out.write_text(json.dumps(log, indent=2))
        print(f"\nrun log -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="as_agent", help="run the role on this agent instead (agy, codex, claude, deepseek)")
    common.add_argument("--approved", action="store_true", help="human approval for an isolated project")
    sub.add_parser("roles").set_defaults(fn=cmd_roles)
    po = sub.add_parser("po", parents=[common]); po.set_defaults(fn=cmd_po)
    po.add_argument("-p", "--project", required=True); po.add_argument("-s", "--sprint", required=True)
    po.add_argument("--context-file"); po.add_argument("request")
    pl = sub.add_parser("plan", parents=[common]); pl.set_defaults(fn=cmd_plan)
    pl.add_argument("-p", "--project", required=True); pl.add_argument("-s", "--sprint", required=True)
    pl.add_argument("--story", required=True)
    d = sub.add_parser("dispatch", parents=[common]); d.set_defaults(fn=cmd_dispatch)
    d.add_argument("--plan", required=True); d.add_argument("--go", action="store_true"); d.add_argument("--cwd")
    a = ap.parse_args()
    try:
        a.fn(a)
    except sf.PlanError as e:
        sys.exit(f"stopped: {e}")


if __name__ == "__main__":
    main()
