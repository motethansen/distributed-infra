"""Code-review handler — runs an agent code review on a local repo path (routed `review`: Claude Sonnet, falling back to Codex/agy on a usage limit)."""
from __future__ import annotations

import os

from shared.models import Task


async def handle_code_review(task: Task) -> dict:
    """
    payload:
      target: str   — local repo path (e.g. ~/Projects/simtrader)
      focus:  str   — optional focus area (e.g. "security", "performance")
      model:  str   — optional model override
    """
    target = task.payload.get("target", "")
    focus  = task.payload.get("focus", "")
    model  = task.payload.get("model", "")

    if not target:
        return {"needs_human": True, "notes": "code_review requires a target path"}

    expanded = os.path.expanduser(target)
    if not os.path.isdir(expanded):
        return {
            "needs_human": True,
            "notes": f"code_review: path not found: {expanded}",
        }

    focus_clause = f" Focus on: {focus}." if focus else ""
    prompt = (
        f"You are doing a thorough code review of the repository at {expanded}.{focus_clause}\n\n"
        "Review for:\n"
        "1. Correctness bugs and logic errors\n"
        "2. Security vulnerabilities (injection, auth, secrets exposure)\n"
        "3. Performance issues\n"
        "4. Code quality: dead code, duplication, unclear naming\n\n"
        "For each finding include: file path, line reference, severity (high/medium/low), "
        "description, and a suggested fix.\n\n"
        "End with a brief summary: overall health, top 3 priorities to fix."
    )

    from agents.runner import run_agent
    result = await run_agent(
        prompt=prompt,
        task_kind="review",
        model=model or None,
        cwd=expanded,
        timeout=600,
    )

    if not result.get("ok"):
        return {
            "needs_human": True,
            "notes": f"{result.get('agent') or 'review'} code review failed: {result.get('error')}",
            "failover": result.get("failover"),
        }
    # `agent` stays the agent that actually did the review (proof of who ran it).
    result["handler"] = "code_review"
    result["target"] = target
    return result
