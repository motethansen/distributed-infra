---
description: What can I ask the desk? Examples of questions and what each one does
allowed-tools: Bash(dis-agent:*)
model: haiku
---
Introduce the desk in at most 10 lines, friendly and plain. Say that I can just talk: ask about any project, a story, what is blocked and why, what the agents did, what it cost, or what to do next, and you will answer from the live board and suggest the exact command when one helps. Give these example questions to copy:

- "What should I do next?"  ·  "What is waiting for my review?"  ·  "Why is BA-091 blocked?"
- "Show me the backlog for vantage"  ·  "Is DI-056 done?"  ·  "What did the agents do today?"
- "Start a sprint for vantage"  ·  "Close DI-056"  ·  "Ask the Scrum Master what to plan first for budgetapp"

Then say which things need my yes (starting a sprint uses Opus; approving, running, closing and cancelling change things) and that you never use --force. Finish by running `dis-agent next` and offering the top suggestion.
