"""What should the human do next? Ranked suggestions, each with the exact command.

Built from the board (the same data Vantage shows) plus the plans and grooming reports on this machine, with no model involved,
so the desk chat can relay them instead of guessing. Every suggestion says whether running it only reads, queues agent work, runs
agents for minutes, or changes the backlog, and what it costs. The chat (and you) decide; nothing here runs anything.
"""
from __future__ import annotations

from typing import Any

READ, QUEUES, RUNS, CHANGES = "reads only", "queues agent work (no backlog change)", "runs agents for minutes, opens PRs", "changes the backlog"


def _statuses(p: dict) -> dict[str, str]:
    v = p.get("backlog_view") or {}
    out = {}
    for sp in v.get("sprints", []):
        for s in sp.get("stories", []):
            if s.get("id"):
                out[s["id"]] = s.get("status")
    for s in (v.get("unscheduled") or []) + (v.get("cancelled") or []):
        out[s["id"]] = s.get("status")
    return out


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _s(project: str, pri: int, title: str, why: str, command: str, kind: str, cost: str = "free") -> dict[str, Any]:
    return {"project": project, "priority": pri, "title": title, "why": why, "command": command, "kind": kind, "cost": cost}


def suggest(board: dict, plans: list[dict], groomed: dict[str, str], project: str | None = None, limit: int = 6,
            proposals: dict[str, dict] | None = None) -> list[dict]:
    """plans: [{project, sprint, sprint_number, story_id, backlog_id, title, approved, ran, path}]; groomed: {project: date}."""
    out: list[dict] = []
    projects = {p["project"]: p for p in board.get("projects", [])}
    digest = board.get("digest") or {}
    covered: set[tuple[str, str]] = set()

    for pl in plans:
        pr = projects.get(pl["project"])
        status = (_statuses(pr) if pr else {}).get(pl.get("backlog_id") or "")
        sprint = pl.get("sprint_number") or pl["sprint"]
        bid = pl.get("backlog_id") or pl["story_id"]
        if pl.get("backlog_id"):
            covered.add((pl["project"], pl["backlog_id"]))
        if status in (None, "done", "superseded", "cancelled") and pl.get("backlog_id"):
            continue                                                                  # closed or gone: nothing to do
        if status == "in_review":
            out.append(_s(pl["project"], 1, f"{bid} is finished and waiting for your sprint review: {pl['title'][:70]}",
                          "the work ran, the tester validated it and the reviewer read it; closing is your decision",
                          f"/close {pl['project']} {sprint} {bid}   (or: dis-agent close -p {pl['project']} -s {sprint} --story {bid} --check)",
                          READ, "free (the preview); closing then changes the backlog"))
        elif pl.get("approved") and not pl.get("ran"):
            out.append(_s(pl["project"], 1, f"{bid} is approved but has not run: {pl['title'][:70]}", "approved plans only run when you say go",
                          f"dis-agent go {pl['path']}", RUNS, "Sonnet/DeepSeek/agy/Codex subscriptions; DeepSeek pennies"))
        elif not pl.get("approved"):
            out.append(_s(pl["project"], 2, f"The plan for {pl['story_id']} is waiting for your approval: {pl['title'][:70]}",
                          "read the task table first (roles, agents, machines); editing the plan voids an approval",
                          f"dis-agent approve {pl['path']}", CHANGES, "free"))

    proposals = proposals or {}
    for pn, pr_ in proposals.items():
        if project and pn != project:
            continue
        out.append(_s(pn, 1, f"A sprint proposal for {pn} is waiting for you: sprint {pr_['sprint']}, {pr_['stories']} stories, {pr_['points']:g} points, "
                              f"{len(pr_['todo'])} not planned yet ({', '.join(pr_['todo'][:5])}{'…' if len(pr_['todo']) > 5 else ''})",
                      "this is the planning meeting: give the Product Owner feedback to revise it, or plan the stories you accept",
                      f'dis-agent po -p {pn} -s {pr_["sprint"]} --revise "<your feedback>"   or   dis-agent plan -p {pn} -s {pr_["sprint"]} --story <ID>',
                      QUEUES, "a revision is one Opus call (about $0.3); plan is Sonnet (cents)"))
    review_total: list[dict] = []
    ungroomed: list[tuple[int, str]] = []
    stalled: list[str] = []
    for pn, pr in projects.items():
        if project and pn != project:
            continue
        v = pr.get("backlog_view") or {}
        waiting = [r for r in digest.get("review_queue", []) if r["project"] == pn and (pn, r["id"]) not in covered]
        review_total += waiting
        if waiting and project:
            out.append(_s(pn, 3, f"{pn}: {_n(len(waiting), 'story is', 'stories are')} marked in review ({', '.join(r['id'] for r in waiting[:4])}{'…' if len(waiting) > 4 else ''})",
                          "no fleet plan is attached, so the evidence is in the backlog and the PRs; closing means editing the backlog",
                          f"/backlog {pn}", READ))
        open_sprint = any((sp.get("points_remaining") or 0) > 0 and sp.get("stories") for sp in v.get("sprints", []))
        open_n = len(v.get("unscheduled") or [])
        has_plan = any(pl["project"] == pn for pl in plans)
        if open_n and not open_sprint and not has_plan and pn not in proposals:
            if pn in groomed:
                out.append(_s(pn, 2, f"{pn} has {_n(open_n, 'open story', 'open stories')} and no sprint running (groomed {groomed[pn]})",
                              "the Product Owner has reviewed the backlog; the next step is to plan a sprint with it",
                              f"/sprint {pn}", QUEUES, "Opus, about $0.3-0.6 per Product Owner call"))
            else:
                ungroomed.append((open_n, pn))
        blind = [x for x in v.get("unscheduled") or [] if x["status"] == "blocked" and not x.get("evidence")]
        if blind:
            out.append(_s(pn, 4, f"{pn}: {_n(len(blind), 'blocked story has', 'blocked stories have')} no reason recorded ({', '.join(x['id'] for x in blind[:4])})",
                          "a blocked story without a reason cannot be unblocked or cancelled sensibly", f"/backlog {pn}", READ))
    if review_total and not project:
        projs = sorted({r["project"] for r in review_total})
        out.append(_s("-", 3, f"{_n(len(review_total), 'story is', 'stories are')} marked in review across {_n(len(projs), 'project')} ({', '.join(projs)})",
                      "no fleet plan is attached to them; read each one's evidence, then it is yours to close", "/review", READ))
    ungroomed.sort(reverse=True)
    for n_open, pn in (ungroomed if project else ungroomed[:1]):
        out.append(_s(pn, 4, f"{pn} has {_n(n_open, 'open story', 'open stories')}, no sprint, and has never been groomed",
                      "grooming checks stories against the architecture before you spend a sprint on them", f"/groom {pn}", QUEUES, "Opus, about $0.3"))
    for d in digest.get("decisions", []):
        if d["kind"] == "stalled" and project and d["project"] == project:
            out.append(_s(d["project"], 4, d["text"], "decide: schedule it, cancel what is dead, or leave it parked on purpose", f"/backlog {d['project']}", READ))
        elif d["kind"] == "stalled":
            stalled.append(d["project"])
    if stalled and not project:
        out.append(_s("-", 5, f"{_n(len(stalled), 'project looks', 'projects look')} stalled ({', '.join(sorted(stalled))})",
                      "nothing closed for two weeks or more while stories stay open: schedule it, cancel what is dead, or park it on purpose", "/scrum", READ))
    o = digest.get("opus")
    if o and o.get("cap") and o["spent"] / o["cap"] >= 0.8 and not project:
        out.append(_s("-", 2, f"Opus budget is {o['spent'] / o['cap']:.0%} used (${o['spent']:.2f} of ${o['cap']:.0f})",
                      "the Product Owner falls back to Sonnet at the cap", "dis-agent roles", READ))
    out.sort(key=lambda x: (x["priority"], x["project"], x["title"]))
    seen, uniq = set(), []
    for x in out:
        if x["title"] not in seen:
            seen.add(x["title"])
            uniq.append(x)
    return uniq[:limit] if limit else uniq


def render(sugs: list[dict]) -> str:
    if not sugs:
        return "Nothing needs you right now.\n"
    L = []
    for i, s in enumerate(sugs, 1):
        L += [f"{i}. {s['title']}", f"   why:     {s['why']}", f"   command: {s['command']}", f"   effect:  {s['kind']}; cost: {s['cost']}", ""]
    return "\n".join(L)


def review_items(board: dict, plans: list[dict], cfg_projects: dict, project: str | None = None) -> list[dict]:
    """Every story waiting for the human's review, with its sprint, its PRs and the exact command that confirms it."""
    from shared import scrum_flow
    out = []
    by_plan = {(pl["project"], pl.get("backlog_id")): pl for pl in plans if pl.get("backlog_id")}
    projects = {p["project"]: p for p in board.get("projects", [])}
    for r in (board.get("digest") or {}).get("review_queue", []):
        pn, sid = r["project"], r["id"]
        if project and pn != project:
            continue
        sprint = None
        for sp in (projects.get(pn, {}).get("backlog_view") or {}).get("sprints", []):
            if any(s.get("id") == sid for s in sp.get("stories", [])):
                sprint = sp.get("sprint")
                break
        slug = scrum_flow.repo_slug((cfg_projects.get(pn) or {}).get("repo"))
        prs = scrum_flow.pr_links(r.get("evidence", ""), slug)
        pl = by_plan.get((pn, sid))
        if pl:
            cmd = f"/close {pn} {pl.get('sprint_number') or pl['sprint']} {sid}"
            how = "built by the fleet: the tester and reviewer ran; this shows their evidence and closes only if every gate passes"
        elif prs:
            cmd = f"dis-agent accept -p {pn} --story {sid} --pr {prs[0]}"
            how = "not built by the fleet: merge the PR(s) first if it is not merged yet; accept checks GitHub that it is merged, then closes"
        else:
            cmd = f'dis-agent accept -p {pn} --story {sid} --evidence "what you verified"'
            how = "not built by the fleet and no PR is named: say what you verified (a response, a test, a screen)"
        out.append({"project": pn, "id": sid, "title": r["title"], "sprint": sprint, "evidence": r.get("evidence", ""), "prs": prs,
                    "fleet": bool(pl), "command": cmd, "how": how})
    return out
