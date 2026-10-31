---
description: Close a finished story after you have seen its validation and review — only when you ask
allowed-tools: Bash(dis-agent:*)
model: haiku
---
$ARGUMENTS is `<project> <sprint> <story>`; the sprint can be the label (S-roles) or the number the board shows (1), and the story is the backlog ID (DI-056) or the plan's ID (S-1). The human is asking you to close it, so:

1. Run `dis-agent close -p <project> -s <sprint> --story <story> --check`. Show him the validation per criterion, the review's findings, the PRs and whether every gate passes.
2. If every gate passes, run `dis-agent close -p <project> -s <sprint> --story <story> --yes` (he asked for it) and report what changed: the backlog row, the sprint file, the burndown.
3. If any gate fails (a task unfinished, no review, validation not a pass) do NOT close. Explain which gate and what would fix it (`dis-agent validate -p <project> -s <sprint> --story <story>` runs the independent tester; add `--branch main` if the story is already merged). Never use --force: only he may, in his own terminal.
