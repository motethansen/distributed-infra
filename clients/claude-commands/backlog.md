---
description: A project's product backlog arranged per sprint, with why each blocked story is blocked
allowed-tools: Bash(dis-agent:*)
model: haiku
---
Run `dis-agent backlog -p $ARGUMENTS` with Bash (the argument is a project name such as budgetapp or distributed-infra; if it is missing, run `dis-agent status` and ask which one). Show it as it is arranged: each sprint with its stories and status, then what is not in a sprint yet (in progress, in review, blocked, planned). For every blocked story say WHY from the "why:" line, and if no reason is recorded say so. Mention cancelled stories only if I ask. Never pass flags that change the backlog.
