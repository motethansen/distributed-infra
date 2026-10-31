---
description: Start a sprint review on demand — what is waiting for my confirmation, and how to confirm each
allowed-tools: Bash(dis-agent:*)
model: haiku
---
Run `dis-agent review` with Bash (add `-p <project>` if $ARGUMENTS names a project). For each waiting story say in one line what it is and what is left to do, using its evidence, PRs and sprint, then give its confirm command. Group them: first the ones that are ready to confirm now (PR merged, or built by the fleet and validated), then the ones that need something from me first (merge a PR, run something on a device, record that a check was run). Be honest: if a story's evidence says work is unfinished, say it is not ready.

If $ARGUMENTS also names a sprint (for example `distributed-infra 1`), run `dis-agent review -p <project> -s <sprint>` for the sprint-level outcome instead.

Confirming is mine to decide. When I tell you to accept or close a specific story, run its confirm command with `--yes`; for `accept --pr` the tool checks GitHub that the PR is merged. Never pass --force.
