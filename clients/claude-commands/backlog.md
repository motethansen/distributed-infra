---
description: A project's open work — in progress, in review, blocked, planned
allowed-tools: Bash(dis-agent:*)
model: haiku
---
Run `dis-agent backlog -p $ARGUMENTS` with Bash (the argument is a project name such as budgetapp or distributed-infra; if it is missing, run `dis-agent status` and ask which one). Show the in-flight stories first, then blocked, then the top planned ones, and say how many more there are.
