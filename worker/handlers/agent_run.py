"""Agent task handler — runs claude/agy/codex from the task queue."""
from __future__ import annotations

from shared.models import Task


def _refused(notes: str, requested) -> dict:
    """A task refused before any agent ran still carries the keys every agent_run result has."""
    return {"needs_human": True, "notes": notes, "answered_by": None, "answered_model": None,
            "requested": requested, "failover": []}


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
      history: list — prior turns [{role: user|assistant, text, agent}] so another
                      agent can take over mid-conversation (failover hand-over)
      failover: bool — default true; false pins the task to its first agent
      role: str     — Scrum role (config/roles.yaml). The orchestrator resolves it when the task is
                      queued: it fills agent/model and sets `_allow_opus` for a role allowed Opus.
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
    history = task.payload.get("history") if isinstance(task.payload.get("history"), list) else None
    failover = task.payload.get("failover", True) is not False
    allow_opus = task.payload.get("_allow_opus") is True      # set by the orchestrator only

    pl = task.payload
    requested = pl["choice"] if isinstance(pl.get("choice"), str) and pl["choice"] else {
        "agent": pl.get("agent"), "model": pl.get("model"), "role": pl.get("role")}
    if not prompt:
        return _refused("prompt is required in payload", requested)
    if not agent and not (task_kind or sensitivity):
        return _refused("either 'agent' or 'task_kind'/'sensitivity' is required", requested)

    from agents.runner import run_agent
    result = await run_agent(agent=agent, prompt=prompt, model=model, cwd=cwd, timeout=timeout,
                             session_id=session_id, resume=resume,
                             task_kind=task_kind, sensitivity=sensitivity,
                             history=history, failover=failover, allow_opus=allow_opus)

    from agents.runner import finish_result
    note = pl.get("_role_note") if isinstance(pl.get("_role_note"), str) else result.get("route_note")
    if note and "->" not in note and result.get("ok") and result.get("agent"):
        # the privacy guard says why deepseek was skipped, not who took over
        note = f"{note} -> {result['agent']}"
    finish_result(result, requested, note)

    if not result.get("ok"):
        return {
            "needs_human": True,
            "notes": f"{result.get('agent') or agent or task_kind} agent failed: {result.get('error')}",
            "failover": result["failover"],
            "answered_by": None,
            "answered_model": None,
            "requested": result["requested"],
            **({"route_note": result["route_note"]} if "route_note" in result else {}),
        }

    return result
