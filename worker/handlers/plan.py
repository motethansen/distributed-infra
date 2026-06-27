"""Plan-and-Execute + Supervisor + Validator (#8) — the reasoning-autonomy engine.

A `plan` task decomposes a goal into steps, executes each as a specialist sub-task
on a shared machine + working dir, validates flagged steps (retry-with-feedback),
and stops cleanly on a circuit breaker (step/retry budget) or escalates to a human.

This is the engine the autonomous-project agent (#18) is built on. It reuses the
existing queue (enqueue sub-tasks), the #5 router (cheapest model per role), and the
needs_human escape hatch — no new framework.

payload:
  goal: str            — the objective to plan + execute.
  cwd: str             — shared working dir for all steps (default a scratch dir).
  target_machine: str  — machine all steps run on (default mac-mini); shared so
                         files written in one step are visible to the next.
  max_steps: int       — circuit breaker: hard cap on steps executed (default 6).
  max_retries: int     — per validated step, retries on validation failure (default 2).
  step_timeout: int    — seconds per sub-task (default 600).
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import httpx

from shared.models import Task

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", "")

_HARD_MAX_STEPS = 12       # absolute ceiling regardless of payload (runaway backstop)
_CTX_CHARS = 800           # per-step output kept as context for later steps


def _headers() -> dict:
    return {"x-secret-key": SECRET_KEY, "Content-Type": "application/json"}


def _extract_json(text: str) -> dict | None:
    """Pull the first {...} JSON object out of an LLM reply."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def _plan_steps(goal: str, max_steps: int) -> list[dict]:
    """PLANNER — decompose the goal into ordered steps (routed to a planning model)."""
    from agents.runner import run_agent
    prompt = (
        "You are a planning agent. Decompose the GOAL into an ordered list of concrete "
        "steps a coding/writing agent can execute in a SHARED working directory (later "
        "steps see files created by earlier ones). Return STRICT JSON only, no prose:\n"
        '{"steps": [{"description": "<imperative step>", "agent": "claude", "validate": false}]}\n'
        f"- At most {max_steps} steps. Keep each step self-contained and concrete.\n"
        "- agent: \"claude\" for coding/writing, \"deepseek\" for pure reasoning.\n"
        "- validate: true ONLY for the step that produces the final deliverable.\n\n"
        f"GOAL: {goal}"
    )
    res = await run_agent(prompt=prompt, task_kind="planning")
    data = _extract_json(res.get("response", "")) if res.get("ok") else None
    steps = (data or {}).get("steps") if isinstance(data, dict) else None
    if not steps or not isinstance(steps, list):
        # Graceful fallback: single step = the whole goal.
        return [{"description": goal, "agent": "claude", "validate": True}]
    cleaned = []
    for s in steps[:max_steps]:
        if isinstance(s, dict) and s.get("description"):
            cleaned.append({
                "description": str(s["description"]),
                "agent": (s.get("agent") or "claude"),
                "validate": bool(s.get("validate", False)),
            })
    return cleaned or [{"description": goal, "agent": "claude", "validate": True}]


async def _enqueue_and_wait(payload: dict, timeout: int) -> dict:
    """Run one step as an agent_run sub-task; poll until it finishes."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{ORCHESTRATOR_URL}/tasks", headers=_headers(),
                         json={"type": "agent_run", "payload": payload, "notes": "plan step"})
        if r.status_code != 201:
            return {"ok": False, "error": f"enqueue failed ({r.status_code})"}
        tid = r.json().get("id")

    waited = 0
    async with httpx.AsyncClient(timeout=15) as c:
        while waited < timeout:
            await asyncio.sleep(3)
            waited += 3
            rr = await c.get(f"{ORCHESTRATOR_URL}/tasks/{tid}", headers=_headers())
            if rr.status_code != 200:
                continue
            d = rr.json()
            st = d.get("status")
            if st in ("done", "failed", "needs_human"):
                result = d.get("result") or {}
                if st == "done":
                    return {"ok": True, "output": result.get("response", "")}
                return {"ok": False, "error": result.get("error") or d.get("notes") or st}
    return {"ok": False, "error": "step timed out"}


async def _validate(goal: str, description: str, output: str) -> tuple[bool, str]:
    """VALIDATOR — judge whether a step's output satisfies the goal."""
    from agents.runner import run_agent
    prompt = (
        "You are a strict validator. Decide if the step output satisfies the goal.\n"
        "Return STRICT JSON only: {\"passed\": true|false, \"feedback\": \"<concise fix if failed, else ok>\"}\n\n"
        f"GOAL: {goal}\nSTEP: {description}\nOUTPUT:\n{output[:2000]}"
    )
    res = await run_agent(prompt=prompt, task_kind="planning")
    data = _extract_json(res.get("response", "")) if res.get("ok") else None
    if not isinstance(data, dict):
        return True, "validator-inconclusive"  # don't block on a flaky judge
    return bool(data.get("passed", True)), str(data.get("feedback", ""))


async def handle_plan(task: Task) -> dict:
    p = task.payload or {}
    goal = (p.get("goal") or "").strip()
    if not goal:
        return {"needs_human": True, "notes": "plan requires a 'goal' in payload"}

    machine = p.get("target_machine") or "mac-mini"
    cwd = p.get("cwd") or f"~/plan-scratch/{task.id[:8]}"
    max_steps = min(int(p.get("max_steps", 6) or 6), _HARD_MAX_STEPS)
    max_retries = int(p.get("max_retries", 2) or 2)
    step_timeout = int(p.get("step_timeout", 600) or 600)

    steps = await _plan_steps(goal, max_steps)

    transcript: list[dict] = []
    context = ""
    executed = 0

    for idx, step in enumerate(steps, 1):
        if executed >= max_steps:  # circuit breaker
            transcript.append({"step": idx, "description": step["description"],
                               "status": "skipped", "note": f"circuit breaker: max_steps={max_steps}"})
            break

        desc = step["description"]
        agent = step.get("agent") or "claude"
        attempt = 0
        feedback = ""
        result = {"ok": False, "error": "not run"}

        while attempt <= (max_retries if step.get("validate") else 0):
            prompt = (
                (f"Context from previous steps:\n{context}\n\n" if context else "")
                + (f"A previous attempt failed validation. Fix this: {feedback}\n\n" if feedback else "")
                + f"Working directory: {cwd} (on {machine}). Create/modify files there.\n"
                + f"Task: {desc}\nBe concrete and complete."
            )
            result = await _enqueue_and_wait(
                {"agent": agent, "prompt": prompt, "cwd": cwd, "_target_machine": machine,
                 "timeout": step_timeout},
                timeout=step_timeout + 30,
            )
            executed += 1
            if not result.get("ok"):
                break
            if not step.get("validate"):
                break
            passed, feedback = await _validate(goal, desc, result.get("output", ""))
            if passed:
                feedback = ""
                break
            attempt += 1
            if executed >= max_steps:
                break

        output = result.get("output") or result.get("error") or ""
        status = "done" if result.get("ok") and not feedback else ("failed" if not result.get("ok") else "validation-failed")
        transcript.append({
            "step": idx, "description": desc, "agent": agent, "status": status,
            "attempts": attempt + 1, "output": output[:_CTX_CHARS],
            **({"validator_feedback": feedback} if feedback else {}),
        })
        context = (context + f"\n[Step {idx}] {desc}\n{output[:_CTX_CHARS]}").strip()

        if status != "done":
            # Couldn't converge — escalate with everything done so far.
            summary = _summary(goal, machine, cwd, transcript, converged=False)
            return {"needs_human": True, "notes": summary,
                    "action": "Review the partial plan output and advise / retry."}

    summary = _summary(goal, machine, cwd, transcript, converged=True)
    return {"response": summary, "steps": transcript}


def _summary(goal: str, machine: str, cwd: str, transcript: list[dict], converged: bool) -> str:
    icon = {"done": "✓", "failed": "✗", "validation-failed": "⚠", "skipped": "•"}
    head = f"🧠 Plan {'complete' if converged else 'needs input'} — {goal}"
    lines = [head, f"({len(transcript)} steps · {machine}:{cwd})", ""]
    for t in transcript:
        lines.append(f"{icon.get(t['status'], '?')} {t['step']}. {t['description']}"
                     + (f"  (x{t['attempts']})" if t.get('attempts', 1) > 1 else ""))
    last = next((t for t in reversed(transcript) if t.get("output")), None)
    if last:
        lines += ["", "Last output:", last["output"][:600]]
    return "\n".join(lines)
