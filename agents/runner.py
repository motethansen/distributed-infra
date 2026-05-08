#!/usr/bin/env python3
"""
Unified agent runner — launch any agent from the CLI or task queue.

CLI usage:
  python agents/runner.py --agent claude  --prompt "hello"
  python agents/runner.py --agent gemini  --prompt "explain decorators"
  python agents/runner.py --agent groq    --prompt "write a sort function"
  python agents/runner.py --agent codex   --prompt "fix this bug"
  python agents/runner.py --test          # smoke-test all available agents
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Callable, Coroutine


AGENTS: dict[str, Callable] = {}


def _load_agents() -> None:
    from agents.claude_agent import run as claude_run
    from agents.gemini_agent import run as gemini_run
    from agents.groq_agent import run as groq_run
    from agents.codex_agent import run as codex_run
    AGENTS["claude"] = claude_run
    AGENTS["gemini"] = gemini_run
    AGENTS["groq"] = groq_run
    AGENTS["codex"] = codex_run


async def run_agent(agent: str, prompt: str, model: str | None = None, cwd: str | None = None) -> dict:
    _load_agents()
    if agent not in AGENTS:
        return {"error": f"Unknown agent: {agent}. Choose from: {list(AGENTS)}", "ok": False}
    kwargs = {"prompt": prompt}
    if model:
        kwargs["model"] = model
    if cwd:
        kwargs["cwd"] = cwd
    return await AGENTS[agent](**kwargs)


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
    parser.add_argument("--agent", choices=["claude", "gemini", "groq", "codex"])
    parser.add_argument("--prompt", default="")
    parser.add_argument("--model", default=None)
    parser.add_argument("--test", action="store_true", help="Smoke-test all agents")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.test:
        asyncio.run(_smoke_test())
        sys.exit(0)

    if not args.agent or not args.prompt:
        parser.print_help()
        sys.exit(1)

    result = asyncio.run(run_agent(args.agent, args.prompt, args.model))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("response") or result.get("error", "no output"))
    sys.exit(0 if result.get("ok") else 1)
