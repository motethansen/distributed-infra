#!/usr/bin/env python3
"""
Unified agent runner — launch any agent from the CLI or task queue.

CLI usage:
  python agents/runner.py --agent claude  --prompt "hello"
  python agents/runner.py --agent agy     --prompt "explain decorators"
  python agents/runner.py --agent codex   --prompt "fix this bug"
  python agents/runner.py --task-kind code --prompt "…"   # routed, with failover
  python agents/runner.py --test          # smoke-test all available agents
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Callable, Coroutine

# Ensure repo root is on sys.path so `from agents.x import` works when this
# script is invoked directly (python agents/runner.py) rather than as a module.
sys.path.insert(0, str(Path(__file__).parent.parent))


AGENTS: dict[str, Callable] = {}


def _load_agents() -> None:
    from agents.claude_agent import run as claude_run
    from agents.agy_agent import run as agy_run
    from agents.codex_agent import run as codex_run
    from agents.deepseek_agent import run as deepseek_run
    from agents.content_agent import run as content_run
    from agents.social_agent import run as social_run
    AGENTS["claude"] = claude_run
    AGENTS["agy"] = agy_run
    AGENTS["codex"] = codex_run
    AGENTS["deepseek"] = deepseek_run
    AGENTS["content"] = content_run
    AGENTS["social"] = social_run


# Agents that support multi-turn conversation resume (caller-supplied session id).
# Others run one-shot and ignore session_id, so we never pass it to them.
RESUMABLE = {"claude"}


# Wrapper agents add their own system prompt and do their own failover inside, so
# the runner never swaps them for a bare agent (that would drop the system prompt).
WRAPPERS = {"content", "social"}


def finish_result(result: dict, requested=None, route_note: str | None = None) -> dict:
    """Stamp the provenance keys every agent_run result carries.

    answered_by/answered_model: who produced the answer (None if nobody did; a '*-default'
    model is kept as the agent reported it). failover: one entry per failed step, [] if none.
    requested: what the caller queued. route_note: only when the cap/privacy guard changed
    the agent or model before the task ran. Existing keys (agent, model, ...) stay as they are.
    """
    ok = bool(result.get("ok"))
    if ok:
        result.setdefault("answered_by", result.get("agent"))
        result.setdefault("answered_model", result.get("model"))
    else:
        result["answered_by"] = None
        result["answered_model"] = None
    result["failover"] = list(result.get("failover") or [])
    if requested is not None:
        result["requested"] = requested
    if route_note:
        result["route_note"] = route_note
    else:
        result.pop("route_note", None)
    return result


async def _run_one(agent: str, model: str, prompt: str, cwd: str | None, timeout: int | None,
                   session_id: str | None, resume: bool, allow_opus: bool = False) -> dict:
    kwargs = {"prompt": prompt}
    if allow_opus and agent == "claude":
        kwargs["allow_opus"] = True
    if model:
        kwargs["model"] = model
    if cwd:
        kwargs["cwd"] = cwd
    if timeout is not None:
        kwargs["timeout"] = timeout
    if session_id and agent in RESUMABLE:
        kwargs["session_id"] = session_id
        kwargs["resume"] = resume
    return await AGENTS[agent](**kwargs)


async def run_agent(agent: str | None = None, prompt: str = "", model: str | None = None,
                    cwd: str | None = None, timeout: int | None = None,
                    session_id: str | None = None, resume: bool = False,
                    task_kind: str | None = None, sensitivity: str | None = None,
                    history: list[dict] | None = None, failover: bool = True,
                    exclude: set[str] | None = None, allow_opus: bool = False) -> dict:
    """Run a prompt on the routed agent, failing over down the chain when an agent
    is unavailable (usage limit, quota, not logged in). See agents/failover.py.

    `history` is the conversation so far ([{role, text, agent}], oldest first) for
    multi-turn chats; it lets a fallback agent pick up mid-conversation and lets a
    resumable agent catch up on turns another agent answered while it was limited.
    The result carries `agent` (who answered) and `failover` (who was skipped, why).
    `exclude` drops agents that cannot do this job (e.g. API-only agents that
    cannot write files).
    """
    _load_agents()
    from agents import failover as fo

    # Routing layer (#5): when the caller routes by task_kind/sensitivity (or omits
    # the agent), resolve agent+model from config/routing.yaml. An explicit agent
    # with no routing hints keeps its own model. The privacy guard always applies.
    if agent and agent in WRAPPERS:
        chain = [(agent, model or "")]
    else:
        from agents.router import route_chain
        if task_kind or sensitivity or not agent:
            chain = route_chain(task_kind=task_kind, sensitivity=sensitivity, agent=agent, model=model)
        else:
            chain = [(agent.strip().lower(), (model or "").strip().lower())]
            chain += [c for c in route_chain(agent=agent)[1:] if c[0] != chain[0][0]]
    if exclude:
        chain = chain[:1] + [c for c in chain[1:] if c[0] not in exclude]
    if not failover:
        chain = chain[:1]
    requested = {"agent": agent, "model": model, "role": None}   # the handler replaces this with the full payload choice
    route_note = None
    if agent and chain and chain[0][0] != agent.strip().lower():      # the privacy guard swapped the pinned agent
        why = f"{sensitivity} data" if sensitivity else f"{task_kind} work" if task_kind else "this request"
        route_note = f"{agent} not used for {why} -> {chain[0][0]}"
    if not chain or chain[0][0] not in AGENTS:
        bad = chain[0][0] if chain else agent
        return finish_result({"error": f"Unknown agent: {bad!r}. Choose from: {list(AGENTS)}", "ok": False}, requested, route_note)
    chain = [c for c in chain if c[0] in AGENTS]
    primary = chain[0][0]

    # Skip agents still cooling down from a limit — unless every one is, in which
    # case try them all anyway (a stale estimate must never block the work).
    usable = [c for c in chain if not fo.cooling_until(c[0])]
    skipped = [{"agent": a, "reason": "limit", "until": fo.cooling_until(a).isoformat()}
               for a, _ in chain if fo.cooling_until(a)]
    if not usable:
        usable, skipped = chain, []

    attempts: list[dict] = list(skipped)
    last: dict = {}
    queue = list(usable)
    while queue:
        a, m = queue.pop(0)
        p, sid, res = prompt, None, False
        if a == primary and a in RESUMABLE and session_id:
            # The agent's own session holds the conversation. If it never answered
            # (its first turn failed over), start the session with the transcript;
            # if others answered while it was out, catch it up on those turns.
            if resume and fo.has_answered(history, a):
                missed = fo.missed_turns(history, a)
                p = fo.handover_prompt(prompt, missed, []) if missed else prompt
                sid, res = session_id, True
            elif history:
                # Its first turn was answered by another agent. That failed attempt
                # may have claimed the session id, so open a fresh one; the result's
                # session_id tells the caller to resume that one from now on.
                p = fo.handover_prompt(prompt, history, [])
                sid, res = str(uuid.uuid4()), False
            else:
                sid, res = session_id, resume
        elif a != primary or history:
            p = fo.handover_prompt(prompt, history, attempts)
        result = await _run_one(a, m, p, cwd, timeout, sid, res, allow_opus)
        last = result
        if result.get("ok"):
            fo.clear(a)
            result.setdefault("agent", a)
            if attempts:
                result["failover"] = attempts
            return finish_result(result, requested, route_note)
        kind = fo.classify(result.get("error"), a, result.get("failure_kind"))
        if kind is None or not failover:
            break  # a real failure — don't repeat side effects on another provider
        entry = {"agent": a, "reason": kind, "error": (result.get("error") or "")[:300]}
        if kind == "limit":
            entry["until"] = fo.mark_limited(a, result.get("error") or "").isoformat()
        attempts.append(entry)
        print(f"[failover] {a} {kind}: {(result.get('error') or '')[:120]!r} — trying next agent", flush=True)
        if kind == "text_only" and "claude" not in [x["agent"] for x in attempts]:
            # The next agent must be able to do the work itself: Claude (Sonnet) first, never a read-only one.
            # Only a Claude that is already in `queue` (so it passed the privacy/exclude/cooldown filters that
            # built `usable`) is promoted; it is never injected.
            if any(c[0] == "claude" for c in queue):
                queue = [("claude", "sonnet")] + [c for c in queue if c[0] != "claude"]

    last = dict(last or {"ok": False, "error": "no agent available"})
    last.setdefault("agent", primary)
    if attempts:
        last["failover"] = attempts
        if all(x.get("reason") in ("limit", "unavailable") for x in attempts) and len(attempts) >= len(chain):
            last["error"] = "every agent in the chain is unavailable: " + "; ".join(
                f"{x['agent']} ({x['reason']}{', until ' + x['until'] if x.get('until') else ''})" for x in attempts)
    last["ok"] = False
    return finish_result(last, requested, route_note)


TEST_PROMPT = "Reply with exactly one sentence: confirm you are working."

async def _smoke_test() -> None:
    _load_agents()
    print("\n=== Agent smoke test ===\n")
    for name in AGENTS:
        print(f"▶ {name} ...", end=" ", flush=True)
        result = await AGENTS[name](prompt=TEST_PROMPT)
        if result.get("ok"):
            preview = (result.get("response") or "")[:120].replace("\n", " ")
            print(f"✓  [{result.get('model','?')}] {preview}")
        else:
            print(f"✗  {result.get('error')}")
    print()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Distributed infra agent runner")
    parser.add_argument("--agent", choices=["claude", "agy", "codex", "deepseek", "content", "social"])
    parser.add_argument("--prompt", default="")
    parser.add_argument("--model", default=None)
    parser.add_argument("--task-kind", default=None, help="Route by kind (e.g. code, reasoning, classify)")
    parser.add_argument("--sensitivity", default=None, help="e.g. private — forces the privacy class")
    parser.add_argument("--test", action="store_true", help="Smoke-test all agents")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.test:
        asyncio.run(_smoke_test())
        sys.exit(0)

    if not args.prompt or not (args.agent or args.task_kind or args.sensitivity):
        parser.print_help()
        sys.exit(1)

    result = asyncio.run(run_agent(agent=args.agent, prompt=args.prompt, model=args.model,
                                   task_kind=args.task_kind, sensitivity=args.sensitivity))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("response") or result.get("error", "no output"))
    sys.exit(0 if result.get("ok") else 1)
