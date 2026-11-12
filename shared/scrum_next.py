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
            proposals: dict[str, dict] | None = None, steps: dict[str, list[dict]] | None = None) -> list[dict]:
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
    for pn, sts in (steps or {}).items():
        if project and pn != project:
            continue
        for st in sts:
            out.append(_s(pn, 1 if st["blocks"] else 3, f"Your step {st['id']} for {pn} sprint {st['sprint']} is waiting: {st['title'][:80]}"
                          + (f" (blocks {', '.join(st['blocks'])})" if st["blocks"] else ""),
                          "agents cannot do this one" + ("; doing it first keeps the stories it blocks from shipping broken" if st["blocks"] else ""),
                          f'dis-agent steps -p {pn} -s {st["sprint"]}   then   dis-agent done -p {pn} -s {st["sprint"]} --story {st["id"]} --evidence "..."',
                          READ + "; marking it done only records your evidence", "free"))
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
        out.append({"item_key": r.get("item_key") or f"{pn}:{sid}", "project": pn, "id": sid, "title": r["title"], "sprint": sprint, "evidence": r.get("evidence", ""), "prs": prs,
                    "fleet": bool(pl), "command": cmd, "how": how})
    return out


# ── what is waiting on the owner: recorded confirms first, then what is ready to accept (DI-064 stage 4) ──

def pending_confirms(rows: list[dict], board: dict, cfg_projects: dict, plans: list[dict] | None = None) -> dict:
    """The owner's recorded-but-not-applied answers, sorted by what each one means.
    accept: a confirm on a review item (the owner's check, then Accept); team: a confirm that asks the team to do something and has not
    been queued yet; other: a confirm on a decision/adjustment; comments: the owner's own words, which only a person can act on.
    Stale confirms (an older suggestion) are listed apart, never treated as the owner's answer."""
    from shared import scrum_flow, scrum_responses as sr
    cur = sr.current_suggestions(board.get("digest") or {})
    review = {it["item_key"]: it for it in review_items(board, plans or [], cfg_projects)}
    eff = sr.effective(rows, cur)
    out = {"accept": [], "team": [], "other": [], "comments": [], "stale": [], "resolved": []}
    state = {}
    for pr_ in board.get("projects", []):
        for sid, st in _statuses(pr_).items():
            state[f"{pr_['project']}:{sid}"] = st
    for r in rows:
        k = r["item_key"]
        if sr.is_stale(r, cur):
            out["stale"].append({"item_key": k, "id": r["id"]})
            continue
        if r["action"] == "comment":
            out["comments"].append({"item_key": k, "response": r["id"], "text": sr.clean(r.get("comment"), 160)})
            continue
        if eff.get(k) is not r:
            continue
        text = sr.clean(r.get("suggestion_text"), 2000)
        project, _, item = k.partition(":")
        entry = {"item_key": k, "project": project, "id": item, "response": r["id"], "text": sr.clean(text, 150)}
        if state.get(k) in ("done", "cancelled", "superseded"):
            out["resolved"].append({"item_key": k, "id": r["id"]})      # the story closed since: nothing left to apply
        elif sr.classify_suggestion(text) == "team":
            q = scrum_flow_quote(text)
            entry["command"] = f'dis-agent queue-story -p {project} --from {item} --response {r["id"]} --text {q}'
            out["team"].append(entry)
        elif k in review:
            it = review[k]
            entry.update(title=it["title"], prs=it["prs"], fleet=it["fleet"], command=it["command"] + (" --yes" if not it["fleet"] else ""))
            out["accept"].append(entry)
        else:
            out["other"].append(entry)
    return out


def scrum_flow_quote(text: str) -> str:
    import shlex
    return shlex.quote(text[:1500])


def ready_to_accept(board: dict, cfg_projects: dict, plans: list[dict], skip_keys: set[str], project: str | None = None, limit: int = 8) -> list[dict]:
    """Review items with a PR named in their evidence that the owner has not confirmed on the board."""
    return [it for it in review_items(board, plans, cfg_projects, project)
            if f"{it['project']}:{it['id']}" not in skip_keys and (it["prs"] or it["fleet"])][:limit]


def render_pending(p: dict, ready: list[dict] | None = None) -> str:
    n = sum(len(p[k]) for k in ("accept", "team", "other", "comments"))
    if not n and not ready and not p.get("resolved"):
        return ""
    L = ["WAITING ON YOU FIRST", ""]
    if n:
        L.append(f"You have {n} answer{'s' if n != 1 else ''} recorded on the board that no one has applied. A Confirm on the board records your answer; "
                 "it does not accept a story. These are what each one needs:")
    if p["accept"]:
        L += ["", f"  Accept-type ({len(p['accept'])}): you confirmed, and each one is yours to check, then accept (Vantage: the Accept button; chat: say \"accept <ID>\"):"]
        for e in p["accept"]:
            L.append(f"    {e['id']} ({e['project']}) {e.get('title','')[:60]}" + (f"  PRs: {', '.join(x.rsplit('/', 1)[-1] for x in e['prs'])}" if e.get("prs") else "  (no PR named: say what you verified)"))
            L.append(f"      {e['command']}")
    if p["team"]:
        L += ["", f"  Team work ({len(p['team'])}): you confirmed a suggestion that asks the team to do something; it becomes a planned story:"]
        for e in p["team"]:
            L += [f"    {e['id']} ({e['project']}): {e['text']}", f"      {e['command']}"]
    if p["other"]:
        L += ["", f"  Decisions ({len(p['other'])}): recorded; tell me what to do about each:"] + [f"    {e['item_key']}: {e['text']}" for e in p["other"]]
    if p["comments"]:
        L += ["", f"  Your comments ({len(p['comments'])}): only a person can act on a comment; tell me what you want done:"] + [f"    {e['item_key']}: {e['text']}" for e in p["comments"]]
    if p["stale"]:
        L += ["", f"  {len(p['stale'])} confirm(s) were for an older suggestion and do not count; ask again on the board."]
    if p.get("resolved"):
        L += ["", f"  {len(p['resolved'])} more confirm(s) are on stories that are already closed: nothing to do ({', '.join(e['item_key'].split(':')[-1] for e in p['resolved'][:12])})."]
    if ready:
        L += ["", f"  Ready to accept, not yet confirmed by you ({len(ready)} shown): finished and merged, waiting for your sign-off:"]
        for it in ready:
            L.append(f"    {it['id']} ({it['project']}) {it['title'][:70]}")
    return "\n".join(L) + "\n\n"


def add_stuck(sugs: list[dict], stuck: dict, limit: int = 6) -> list[dict]:
    """DI-080: one ranked suggestion 'N stories look stuck in progress', with the three exact commands for the first few of them.
    Ranked just under the finished-and-waiting items (priority 1); a ranked list is cut to `limit` after adding it."""
    from shared import stuck_stories
    items = stuck.get("stuck") or []
    if not items:
        return sugs
    cmds = []
    for it in items[:5]:
        c = stuck_stories.commands(it)
        cmds.append(f"{it['project']} {it['id']} ({it['age']}):\n      accept: {c['accept']}\n      re-run: {c['rerun'] or '(no plan file)'}\n      cancel: {c['cancel']}")
    s = _s(items[0]["project"], 1, f"{_n(len(items), 'story looks', 'stories look')} stuck in progress",
           "in progress for more than 24 hours and no fleet task is running or queued; the run probably died. Check whether the work is merged, then accept, re-run or cancel",
           "\n   ".join(cmds), CHANGES)
    out = sorted(list(sugs) + [s], key=lambda x: x["priority"])      # stable: ties keep their order, this one goes last among priority 1
    return out[:limit] if limit else out
