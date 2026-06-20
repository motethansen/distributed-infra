---
description: Generate a short social post via social_agent (Groq). Supports LinkedIn (default) or Twitter/X thread format.
---

Queue a write_post task on mac-mini for the following topic:

$ARGUMENTS

Parse the arguments:
- Everything before `--format=` is the topic/prompt
- `--format=linkedin` (default) or `--format=twitter`

Steps:
1. POST to the orchestrator at $ORCHESTRATOR_URL/tasks (default: http://100.97.176.37:8000) with:
   - type: write_post
   - payload: { "prompt": "<topic>", "format": "<linkedin|twitter>", "_target_machine": "mac-mini" }
   - notes: "post/<format>: <first 65 chars of topic>"
2. Print the task ID and confirm queued.

Use the secret key from INFRA_SECRET_KEY env var in the x-secret-key header.
