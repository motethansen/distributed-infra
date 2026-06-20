---
description: Queue a long-form article draft via content_agent (Claude/Sonnet). Saves to ~/Articles/<slug>.md and replies via WhatsApp when done.
---

Queue a write_article task on mac-mini for the following topic:

$ARGUMENTS

Use the content_agent (Claude Sonnet). The draft will be saved to ~/Articles/ as a markdown file.

Steps:
1. POST to the orchestrator at $ORCHESTRATOR_URL/tasks with:
   - type: write_article
   - payload: { "prompt": "<topic>", "_target_machine": "mac-mini" }
   - notes: "article: <first 70 chars of topic>"
2. Print the task ID and confirm it was queued.

If ORCHESTRATOR_URL is not set, default to http://100.97.176.37:8000.
Use the secret key from INFRA_SECRET_KEY env var in the x-secret-key header.
