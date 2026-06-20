"""Write-post handler — generates a short social post via social_agent (Groq)."""
from __future__ import annotations

from shared.models import Task


async def handle_write_post(task: Task) -> dict:
    """
    payload:
      prompt: str    — post topic / brief
      format: str    — linkedin | twitter (default: linkedin)
      model:  str    — optional Groq model override
    """
    prompt = task.payload.get("prompt", "")
    format = task.payload.get("format", "linkedin")
    model  = task.payload.get("model", "")

    if not prompt:
        return {"needs_human": True, "notes": "write_post requires a prompt"}

    from agents.social_agent import run
    result = await run(prompt=prompt, model=model, format=format)

    if not result.get("ok"):
        return {
            "needs_human": True,
            "notes": f"social_agent failed: {result.get('error')}",
        }
    return result
