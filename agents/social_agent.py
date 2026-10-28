#!/usr/bin/env python3
"""
Social agent — short-form writing for LinkedIn and X/Twitter.
Routed as a `draft` task (bulk class: DeepSeek first, then Claude Haiku, Codex,
Antigravity on failover); optimised for punchy posts under 280 characters or
short LinkedIn updates under 1300 characters.

CLI usage:
  python agents/social_agent.py "3 lessons from running 4 AI agents in parallel"
  python agents/social_agent.py "..." --format linkedin
"""
from __future__ import annotations

import asyncio
import os
import sys

_SOCIAL_SYSTEM_PROMPT = """You are a concise technology writer creating social media posts.

When asked for a LinkedIn post (default):
- 150-300 words max
- Hook in the first line (no emoji clutter)
- 3-5 short paragraphs
- One clear takeaway at the end
- Finish with 3-5 relevant hashtags on their own line
- No "Excited to share", "Game-changer", or empty hype

When asked for a Twitter/X thread:
- Up to 5 tweets, each under 280 characters
- Number them (1/5, 2/5 …)
- First tweet is the hook — make it stand alone
- No filler tweets

Output ONLY the post text. No meta-commentary, no "here's your post:".
"""


async def run(
    prompt: str,
    model: str = "",
    cwd: str | None = None,
    timeout: int | None = None,
    format: str = "linkedin",
) -> dict:
    try:
        from agents.runner import run_agent
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agents.runner import run_agent

    full_prompt = (
        f"{_SOCIAL_SYSTEM_PROMPT}\n\n"
        f"Format: {format}\n\n"
        f"Topic: {prompt}"
    )

    result = await run_agent(
        task_kind="draft",
        prompt=full_prompt,
        model=model or None,
        timeout=timeout or 180,
    )
    if result.get("ok"):
        result["answered_by"] = result.get("agent")
        result["agent"] = "social"
        result["format"] = format
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    parser.add_argument("--format", default="linkedin", choices=["linkedin", "twitter"])
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model, format=args.format))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
