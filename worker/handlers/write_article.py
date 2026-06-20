"""Write-article handler — queues a long-form draft via content_agent."""
from __future__ import annotations

from shared.models import Task


async def handle_write_article(task: Task) -> dict:
    """
    payload:
      prompt: str   — article title / brief
      model:  str   — optional model override (default: sonnet)
      cwd:    str   — output directory (default: ~/Articles)
    """
    prompt = task.payload.get("prompt", "")
    model  = task.payload.get("model", "")
    cwd    = task.payload.get("cwd")

    if not prompt:
        return {"needs_human": True, "notes": "write_article requires a prompt"}

    from agents.content_agent import run
    result = await run(prompt=prompt, model=model, cwd=cwd)

    if not result.get("ok"):
        return {
            "needs_human": True,
            "notes": f"content_agent failed: {result.get('error')}",
        }
    return result
