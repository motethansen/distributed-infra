---
description: Queue a code review task on mac-mini. Claude reviews the target repo for bugs, security issues, and quality problems.
---

Queue a code_review task on mac-mini for:

$ARGUMENTS

Parse the arguments:
- The path or repo target (everything before any `--focus=` flag)
- Optional `--focus=<area>` (e.g. security, performance, architecture)

Steps:
1. POST to the orchestrator at $ORCHESTRATOR_URL/tasks (default: http://100.97.176.37:8000) with:
   - type: code_review
   - payload: { "target": "<path>", "focus": "<focus or empty>", "_target_machine": "mac-mini" }
   - notes: "review: <target>"
2. Print the task ID and confirm queued.

Use INFRA_SECRET_KEY in the x-secret-key header.
Results will arrive as a WhatsApp reply when the review finishes (up to 10 min for large repos).
