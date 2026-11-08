"""Accepting a story from the board (DI-064 stage 4): the preview, and the digest that ties a click to exactly that preview.

`accept_preview` says what an Accept would change before it changes anything: the story, its PRs and whether GitHub shows each one merged,
what will be written (backlog row to done, sprint file, burndown), and what blocks it. `digest` hashes the facts the owner was shown;
POST /scrum/accept refuses unless the digest sent back equals the digest of a fresh preview, so a story that changed between the preview
and the click is not accepted on the old picture. No model is involved and nothing here writes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from shared import scrum_flow


def _find(board: dict, project: str, story: str) -> tuple[dict | None, dict | None]:
    digest = board.get("digest") or {}
    entry = next((r for r in digest.get("review_queue", []) if r.get("project") == project and r.get("id") == story), None)
    proj = next((p for p in board.get("projects", []) if p.get("project") == project), None)
    return entry, proj


def preview_digest(p: dict) -> str:
    """Hash of what the owner sees. Not the board's as_of: only a change to this story's own facts invalidates it."""
    material = {"project": p["project"], "story": p["story"], "status": p["status"], "title": p["title"],
                "evidence": p["evidence"], "prs": [[x["url"], x["state"]] for x in p["prs"]]}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def accept_preview(board: dict, project: str, story: str, cfg_projects: dict,
                   pr_state: Callable[[str], dict] | None = None) -> dict:
    """pr_state(url) -> {state, merged_at, title} or raises scrum_flow.PlanError (injected so tests never reach GitHub)."""
    entry, proj = _find(board, project, story)
    if entry is None:
        return {"ok": False, "project": project, "story": story, "blockers": [f"{story} is not in the review queue of {project}: "
                "it is not marked in review (already accepted, or still being worked on)"], "prs": [], "will_change": [], "digest": None}
    evidence = entry.get("evidence") or ""
    slug = scrum_flow.repo_slug((cfg_projects.get(project) or {}).get("repo"))
    urls = scrum_flow.pr_links(evidence, slug)
    prs, blockers = [], []
    for u in urls:
        try:
            info = (pr_state or _gh_state)(u)
            prs.append({"url": u, "state": "MERGED" if info.get("merged_at") else info.get("state", "UNKNOWN"),
                        "merged_at": info.get("merged_at"), "title": (info.get("title") or "")[:100]})
        except scrum_flow.PlanError as e:
            msg = str(e)
            prs.append({"url": u, "state": "OPEN" if "not merged" in msg else "UNKNOWN", "merged_at": None, "title": ""})
            blockers.append(f"{u}: {msg}" + (" (merge it first; merging is yours, the desk does not merge)" if "not merged" in msg else ""))
    if not urls:
        blockers.append("no PR is named in this story's evidence, so there is nothing for GitHub to confirm. Accept it from the chat with "
                        "what you verified: dis-agent accept -p %s --story %s --evidence \"...\"" % (project, story))
    status = "in_review"
    sprint = None
    for sp in ((proj or {}).get("backlog_view") or {}).get("sprints", []):
        if any(s.get("id") == story for s in sp.get("stories", [])):
            sprint = sp.get("sprint")
            break
    will = [f"{project}/product-backlog.md: {story} 🔍 in review -> ✅ done (evidence: {', '.join('#' + u.rsplit('/', 1)[-1] for u in urls) or 'none'} merged, accepted by you)"]
    if sprint:
        will.append(f"{project}/sprint-{int(sprint):02d}.md: {story} done, burndown updated" if str(sprint).isdigit() else f"sprint {sprint}: {story} done")
    will.append("both files are committed and pushed to vizneoprojects, and the board refreshes")
    p = {"ok": not blockers, "project": project, "story": story, "title": entry.get("title", ""), "status": status,
         "evidence": evidence[:1200], "prs": prs, "sprint": sprint, "will_change": will, "blockers": blockers,
         "not_changed": "nothing is merged, deployed or run; accepting only records your sign-off"}
    p["digest"] = preview_digest(p)
    return p


def _gh_state(url: str) -> dict:
    import subprocess
    try:
        r = subprocess.run(["gh", "pr", "view", url, "--json", "state,mergedAt,title"], capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.SubprocessError) as e:
        raise scrum_flow.PlanError(f"could not ask GitHub about {url}: {type(e).__name__}") from e
    if r.returncode != 0:
        raise scrum_flow.PlanError(f"could not ask GitHub about {url}: {(r.stderr or r.stdout).strip()[:160]}")
    info = json.loads(r.stdout)
    if info.get("state") != "MERGED":
        raise scrum_flow.PlanError(f"{url} is {info.get('state')}, not merged")
    return {"state": "MERGED", "merged_at": (info.get("mergedAt") or "")[:10], "title": info.get("title", "")}
