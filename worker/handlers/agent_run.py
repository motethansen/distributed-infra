"""Agent task handler — runs claude/agy/codex from the task queue."""
from __future__ import annotations

from shared.models import Task


async def handle_agent_run(task: Task) -> dict:
    """
    payload:
      agent: str    — claude | agy | codex
      prompt: str   — the prompt to send
      model: str    — optional model override
      cwd: str      — working directory for the agent (e.g. ~/Projects/motethansen-site)
      session_id: str — optional multi-turn session id (resumable agents only)
      resume: bool  — continue an existing session_id rather than starting it
    """
    agent = task.payload.get("agent", "")
    prompt = task.payload.get("prompt", "")
    model = task.payload.get("model")
    cwd   = task.payload.get("cwd")
    timeout = task.payload.get("timeout")
    session_id = task.payload.get("session_id")
    resume = bool(task.payload.get("resume", False))

    if not agent or not prompt:
        return {"needs_human": True, "notes": "agent and prompt are required in payload"}

    from agents.runner import run_agent
    result = await run_agent(agent=agent, prompt=prompt, model=model, cwd=cwd, timeout=timeout,
                             session_id=session_id, resume=resume)

    if not result.get("ok"):
        return {
            "needs_human": True,
            "notes": f"{agent} agent failed: {result.get('error')}",
        }

    return result
