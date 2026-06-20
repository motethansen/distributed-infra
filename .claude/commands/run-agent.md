---
description: Queue an agent_run task on the fleet. Syntax: run-agent <agent> <prompt> [--machine=X]
---

Queue an agent_run task with the following arguments:

$ARGUMENTS

Parse:
- First word: agent name (claude | agy | codex | groq | content | social)
- Optional `--machine=<name>` flag (default: mac-mini)
- Everything else: the prompt

Steps:
1. POST to orchestrator at $ORCHESTRATOR_URL/tasks (default: http://100.97.176.37:8000):
   - type: agent_run
   - payload: { "agent": "<agent>", "prompt": "<prompt>", "_target_machine": "<machine>" }
   - notes: first 80 chars of prompt
2. Print task ID and confirm.

Use INFRA_SECRET_KEY in x-secret-key header.
