#!/usr/bin/env python3
"""
Content agent — Claude-backed long-form writing for Medium and Substack.
Wraps claude_agent with a writing system prompt.

Output is saved to CONTENT_OUTPUT_DIR (default ~/Articles/) as a markdown file,
and the article text is also returned in the response for WhatsApp previews.

CLI usage:
  python agents/content_agent.py "How distributed AI agents change indie dev"
"""
from __future__ import annotations

import asyncio
import os
import sys

CONTENT_OUTPUT_DIR = os.environ.get("CONTENT_OUTPUT_DIR", "~/Articles")

_WRITING_SYSTEM_PROMPT = """You are a sharp, opinionated technology writer publishing on Medium and Substack.

Style rules:
- Write for a technical but non-academic audience: founders, developers, indie hackers
- Open with a concrete scene or surprising claim — no "In today's digital landscape"
- Use short paragraphs (2-4 sentences). Vary sentence length for rhythm.
- Subheadings every 200-300 words. Make them punchy, not descriptive.
- Concrete examples over abstract claims. Show, don't tell.
- End with a clear takeaway or open question that invites comments.
- Target 800-1200 words for a standard post.
- Format in clean Markdown (## for H2, **bold** sparingly, no excessive bullet lists).

Do not include: generic intros, "In conclusion", fluff filler, or self-referential notes
about the writing process.

After writing the article, save it to {output_dir}/{{slug}}.md where {{slug}} is a
kebab-case version of the title (e.g. distributed-agents-indie-dev.md).
Then print the full article to stdout so it can be returned as a response."""


async def run(
    prompt: str,
    model: str = "",
    cwd: str | None = None,
    timeout: int | None = None,
) -> dict:
    try:
        from agents.claude_agent import run as claude_run
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agents.claude_agent import run as claude_run

    output_dir = os.path.expanduser(cwd or CONTENT_OUTPUT_DIR)
    os.makedirs(output_dir, exist_ok=True)

    system = _WRITING_SYSTEM_PROMPT.format(output_dir=output_dir)
    full_prompt = f"{system}\n\nWrite an article about: {prompt}"

    result = await claude_run(
        prompt=full_prompt,
        model=model or "sonnet",
        cwd=output_dir,
        timeout=timeout or 600,
    )
    if result.get("ok"):
        result["agent"] = "content"
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default="")
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt, args.model, args.cwd))
    print(result.get("response") or result.get("error"))
    sys.exit(0 if result.get("ok") else 1)
