---
description: Start a sprint review on demand — what is waiting for my confirmation
allowed-tools: Bash(dis-agent:*)
model: haiku
---
Run `dis-agent review` with Bash and show me the queue (project, ID, title, the evidence line). If $ARGUMENTS names a project and sprint (for example `distributed-infra S-roles`), run `dis-agent review -p <project> -s <sprint>` and summarise what ran, the validation result per criterion, the pull requests to look at, and anything stuck. Closing a story needs my own yes in a terminal: tell me to run `dis-agent close -p <project> -s <sprint>`; never pass --yes yourself.
