"""Agent task handler — runs claude/agy/codex from the task queue."""
from __future__ import annotations

from shared.models import Task


async def handle_agent_run(task: Task) -> dict:
    """
    payload:
      agent: str    — claude | agy | codex | deepseek | … (optional if task_kind given)
      prompt: str   — the prompt to send
      model: str    — optional model override
      task_kind: str   — optional; route to the cheapest-fit agent/model via #5
                         (e.g. code, reasoning, classify). Used when agent is omitted.
      sensitivity: str — optional; e.g. "private" forces the privacy class (never
                         routes personal data to a non-private provider).
      cwd: str      — working directory for the agent (e.g. ~/Projects/motethansen-site)
      session_id: str — optional multi-turn session id (resumable agents only)
      resume: bool  — continue an existing session_id rather than starting it
    """
    agent = task.payload.get("agent") or None
    prompt = task.payload.get("prompt", "")
    model = task.payload.get("model")
    task_kind = task.payload.get("task_kind")
    sensitivity = task.payload.get("sensitivity")
    cwd   = task.payload.get("cwd")
    timeout = task.payload.get("timeout")
    session_id = task.payload.get("session_id")
    resume = bool(task.payload.get("resume", False))

    if not prompt:
        return {"needs_human": True, "notes": "prompt is required in payload"}
    if not agent and not (task_kind or sensitivity):
        return {"needs_human": True, "notes": "either 'agent' or 'task_kind'/'sensitivity' is required"}

    from agents.runner import run_agent
    result = await run_agent(agent=agent, prompt=prompt, model=model, cwd=cwd, timeout=timeout,
                             session_id=session_id, resume=resume,
                             task_kind=task_kind, sensitivity=sensitivity)

    if not result.get("ok"):
        return {
            "needs_human": True,
            "notes": f"{result.get('agent') or agent or task_kind} agent failed: {result.get('error')}",
        }

    return result
